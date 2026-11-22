"""web/ için saldırgan testler: doğrulama hataları, XSS, CSV, filtre+sayfalama, HTMX parçaları, güvenli varsayılanlar."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from stajbot import services
from stajbot.config import get_settings
from stajbot.enums import Actor, AppStatus, Direction, EventType, MailStatus, MatchMethod, ReviewStatus, Sentiment
from stajbot.mail import sender as mail_sender
from stajbot.models import Company, CompanySource, EmailMessage, Event
from stajbot.web.app import WEB_DIR

# Ortak fikstürler (bellek içi engine + TestClient + örnek veri) test_web.py'den alınır.
from tests.test_web import client, seeded  # noqa: F401  (pytest fikstürleri ad ile bulur)

TEMPLATES_DIR = WEB_DIR / "templates"
HX = {"HX-Request": "true"}


def _S(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _is_fragment(text: str) -> bool:
    return "<html" not in text.lower() and "<!doctype" not in text.lower()


# --- doğrulama: 422 JSON yerine Türkçe 400 / 404 -----------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ["/sirketler?min_score=", "/sirketler?page=abc", "/sirketler?page=-3", "/sirketler?min_score=abc&sort=yok", "/mailler?company=", "/mailler?company=abc", "/inceleme?only_email=", "/sirketler?page=999999"],
)
def test_lenient_query_params_render_200(client, seeded, path):
    """Filtre formu boş sayı alanı gönderir (min_score=): 422 JSON değil, sayfa çizilmeli."""
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:200])
    assert "<html" in r.text


def test_non_integer_ids_are_404_not_422(client, seeded):
    for path in ["/sirketler/abc", "/sirketler/1.5", "/mailler/abc/onayla", "/gelen/x/bagla"]:
        r = client.get(path) if path.startswith("/sirketler") else client.post(path, data={"company_id": "1"})
        assert r.status_code == 404, (path, r.status_code, r.text[:200])
        assert "bulunamadı" in r.text and "Input should be" not in r.text


def test_missing_or_invalid_form_fields_are_400_turkish(client, seeded):
    a = seeded["a"]
    cases = [
        (f"/sirketler/{a}/durum", {}),  # status eksik
        (f"/sirketler/{a}/not", {}),  # body eksik
        (f"/sirketler/{a}/kisi", {}),  # email eksik
        (f"/gelen/{seeded['orphan']}/bagla", {"company_id": ""}),  # boş select
        (f"/gelen/{seeded['inb']}/duygu", {}),  # sentiment eksik
        ("/ayarlar/profil", {}),  # text eksik
        (f"/mailler/{seeded['out']}/duzenle", {"subject": "x"}),  # body_text eksik
    ]
    for path, data in cases:
        r = client.post(path, data=data)
        assert r.status_code == 400, (path, r.status_code, r.text[:200])
        assert "Geçersiz" in r.text and "Field required" not in r.text and "<html" in r.text
        rh = client.post(path, data=data, headers=HX)
        assert rh.status_code == 400 and _is_fragment(rh.text) and "Hata 400" in rh.text


def test_unknown_route_404_is_turkish(client):
    r = client.get("/boyle-bir-sayfa-yok")
    assert r.status_code == 404
    assert "Not Found" not in r.text and "Sayfa bulunamadı" in r.text
    rh = client.get("/boyle-bir-sayfa-yok", headers=HX)
    assert rh.status_code == 404 and _is_fragment(rh.text) and "Not Found" not in rh.text


def test_method_not_allowed_turkish(client, seeded):
    r = client.get(f"/sirketler/{seeded['a']}/durum")
    assert r.status_code == 405 and "Method Not Allowed" not in r.text and "desteklenmiyor" in r.text


# --- XSS -------------------------------------------------------------------------------------------------------

@pytest.fixture()
def evil(engine):
    S = _S(engine)
    with S() as s:
        c = Company(
            name='<script>alert("ad")</script> Kötü A.Ş.',
            city="Konya",
            sector="<b>sektör</b>",
            description="<img src=x onerror=alert(1)>",
            notes="<svg onload=alert(2)>",
            linkedin_url="javascript:alert('li')",
            career_url="javascript:alert('career')",
            website="https://kotu.example",
            review_status=ReviewStatus.pending,
            score=90,
        )
        s.add(c)
        s.flush()
        s.add(CompanySource(company=c, source="csv", source_url="javascript:alert('src')", raw={"k": "<script>x</script>"}))
        services.add_contact(s, c, "ik@kotu.example", name="<i>Ad</i>", source="t")
        services.add_note(s, c, "<script>alert('not')</script>")
        inb = EmailMessage(
            company=c, direction=Direction.inbound, status=MailStatus.received, from_addr="<b>x</b>@kotu.example",
            subject="<script>alert('konu')</script>", body_text="<script>alert('govde')</script>\nmaalesef",
            matched_by=MatchMethod.domain, received_at=datetime(2026, 10, 3),
        )
        orphan = EmailMessage(direction=Direction.inbound, status=MailStatus.received, from_addr="a@b.c", subject="<script>o</script>", body_text="<script>alert('sahipsiz')</script>", matched_by=MatchMethod.none)
        draft = EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.draft, to_addr="ik@kotu.example", subject="<script>s</script>", body_text="<script>alert('taslak')</script>", tracking_code=c.tracking_code)
        s.add_all([inb, orphan, draft])
        s.commit()
        return {"c": c.id, "inb": inb.id, "orphan": orphan.id, "draft": draft.id}


def test_xss_escaped_everywhere(client, evil):
    cid = evil["c"]
    pages = ["/", "/sirketler", "/sirketler?q=K%C3%B6t%C3%BC", f"/sirketler/{cid}", "/inceleme", "/gelen", "/gelen?tab=unmatched", "/mailler", "/istatistik"]
    for p in pages:
        t = client.get(p).text
        assert "<script>" not in t, p
        assert "<img src=x onerror" not in t and "<svg onload" not in t, p  # ham etiket yok, yalnızca &lt;img ...&gt; metni
        if p != "/istatistik":  # istatistik sayfası şirket adını göstermez
            assert "&lt;script&gt;" in t, p
    assert "&lt;b&gt;sektör&lt;/b&gt;" in client.get("/istatistik").text
    # HTMX parçaları da kaçışlı
    frag = client.post(f"/inceleme/{cid}/sonra", headers=HX).text
    assert _is_fragment(frag) and "<script>" not in frag and "&lt;script&gt;" in frag
    card = client.post(f"/mailler/{evil['draft']}/duzenle", data={"subject": "<script>k</script>", "body_text": "<script>g</script>"}, headers=HX).text
    assert _is_fragment(card) and "<script>" not in card and "&lt;script&gt;" in card
    # Sorgu parametresinden gelen flash metni de kaçışlı
    t = client.get("/sirketler?mesaj=%3Cscript%3Ealert(9)%3C/script%3E").text
    assert "<script>alert(9)" not in t and "&lt;script&gt;alert(9)" in t


def test_javascript_hrefs_never_rendered(client, evil):
    t = client.get(f"/sirketler/{evil['c']}").text
    assert "javascript:" not in t.replace("javascript:alert", "") or 'href="javascript:' not in t
    assert 'href="javascript:' not in t and "href='javascript:" not in t
    for p in ["/sirketler", "/inceleme"]:
        assert 'href="javascript:' not in client.get(p).text


def test_edit_fields_rejects_javascript_urls_and_keeps_newlines(client, seeded, engine):
    a = seeded["a"]
    r = client.post(
        f"/sirketler/{a}/alanlar",
        data={"linkedin_url": "javascript:alert(1)", "career_url": "JAVASCRIPT:alert(2)", "website": "javascript:alert(3)", "priority": "", "employee_count": "abc", "notes": "satır 1\r\nsatır 2"},
        follow_redirects=True,
    )
    assert r.status_code == 200 and "geçersiz" in r.text
    with _S(engine)() as s:
        c = s.get(Company, a)
        assert c.linkedin_url is None and c.career_url is None
        assert c.website == "https://alfa.com.tr"  # geçersiz adres mevcut değeri silmez
        assert c.priority == 3  # boş öncelik -> 422 değil, eski değer
        assert c.employee_count is None
        assert c.notes == "satır 1\nsatır 2"
    # düzgün adresler şema eklenerek kaydedilir, boş alan temizler
    client.post(f"/sirketler/{a}/alanlar", data={"linkedin_url": "linkedin.com/company/alfa", "career_url": "", "priority": "2", "notes": ""})
    with _S(engine)() as s:
        c = s.get(Company, a)
        assert c.linkedin_url == "https://linkedin.com/company/alfa" and c.career_url is None and c.priority == 2 and c.notes is None


# --- durum değişikliği -------------------------------------------------------------------------------------------

def test_status_change_writes_event_with_note_and_warns_on_manual_transition(client, seeded, engine):
    a = seeded["a"]  # status: ready (onaylı + e-posta)
    r = client.post(f"/sirketler/{a}/durum", data={"status": "interview", "note": "  12 Mart   saat 14:00 "}, follow_redirects=True)
    assert r.status_code == 200
    assert "Durum güncellendi" in r.text and "Uyarı" in r.text and "Mülakat" in r.text
    with _S(engine)() as s:
        c = s.get(Company, a)
        ev = [e for e in c.events if e.type == EventType.status_change and e.new_status == AppStatus.interview]
        assert len(ev) == 1
        assert ev[0].body == "12 Mart saat 14:00" and ev[0].created_by == Actor.user and ev[0].old_status == AppStatus.ready
    # sistem akışındaki geçiş uyarı vermez
    r = client.post(f"/sirketler/{seeded['c']}/durum", data={"status": "replied"}, follow_redirects=True)
    assert "Durum güncellendi" in r.text and "Uyarı" not in r.text
    # aynı duruma "geçiş" Event üretmez, not varsa not yazılır
    with _S(engine)() as s:
        before = len(s.get(Company, a).events)
    r = client.post(f"/sirketler/{a}/durum", data={"status": "interview", "note": ""}, follow_redirects=True)
    assert "değişmedi" in r.text
    r = client.post(f"/sirketler/{a}/durum", data={"status": "interview", "note": "ek bilgi"}, follow_redirects=True)
    assert "not eklendi" in r.text
    with _S(engine)() as s:
        c = s.get(Company, a)
        assert len(c.events) == before + 1 and c.events[0].type == EventType.note and c.events[0].body == "ek bilgi"
        assert c.status == AppStatus.interview
    # zaman çizelgesinde İngilizce enum değerleri görünmez
    t = client.get(f"/sirketler/{a}").text
    assert ">status_change<" not in t and ">user<" not in t and ">system<" not in t and ">manual<" not in t


def test_deleting_last_contact_demotes_ready_to_found(client, seeded, engine):
    a = seeded["a"]
    with _S(engine)() as s:
        c = s.get(Company, a)
        assert c.status == AppStatus.ready
        cid = c.contacts[0].id
    r = client.post(f"/sirketler/{a}/kisi/{cid}/sil", follow_redirects=True)
    assert r.status_code == 200 and "Bulundu" in r.text
    with _S(engine)() as s:
        c = s.get(Company, a)
        assert c.contacts == [] and c.status == AppStatus.found
    # başka şirketin kişisi bu şirket üzerinden silinemez / birincil yapılamaz
    with _S(engine)() as s:
        other_cid = s.get(Company, seeded["c"]).contacts[0].id
    assert client.post(f"/sirketler/{a}/kisi/{other_cid}/sil").status_code == 404
    assert client.post(f"/sirketler/{a}/kisi/{other_cid}/birincil").status_code == 404
    with _S(engine)() as s:
        assert len(s.get(Company, seeded["c"]).contacts) == 1


# --- CSV -----------------------------------------------------------------------------------------------------------

def test_csv_bom_quoting_and_turkish(client, engine):
    with _S(engine)() as s:
        c = Company(name='Nokta;Virgül, "Tırnak" A.Ş.', city="İzmir", sector="Çok\nsatırlı", review_status=ReviewStatus.approved, tags=["a,b", "ç"], tech_stack=["C#", "Go"])
        d = Company(name="Düz", city="Ankara", status=AppStatus.sent, review_status=ReviewStatus.rejected)
        s.add_all([c, d])
        s.commit()
    r = client.get("/sirketler.csv")
    assert r.status_code == 200
    assert r.content.startswith("﻿".encode("utf-8"))
    assert "charset=utf-8" in r.headers["content-type"] and "attachment" in r.headers["content-disposition"]
    body = r.content.decode("utf-8-sig")
    assert "\r\n" in body
    import csv
    import io

    rows = list(csv.reader(io.StringIO(body), delimiter=";"))
    header, data = rows[0], rows[1:]
    assert header[:3] == ["id", "ad", "sehir"] and len(data) == 2
    by_name = {row[1]: row for row in data}
    assert 'Nokta;Virgül, "Tırnak" A.Ş.' in by_name  # ; ve " doğru kaçışlanmış
    row = by_name['Nokta;Virgül, "Tırnak" A.Ş.']
    assert row[header.index("sektor")] == "Çok\nsatırlı"
    assert row[header.index("etiketler")] == "a,b,ç" and row[header.index("teknolojiler")] == "C#,Go"
    assert row[header.index("inceleme")] == "Onaylı" and row[header.index("durum")] == "Bulundu"  # Türkçe etiketler
    assert by_name["Düz"][header.index("inceleme")] == "Reddedildi" and by_name["Düz"][header.index("durum")] == "Gönderildi"
    # ham metinde kaçışlanmamış tırnak yok
    assert '"Tırnak"' not in body.split("\r\n")[1].replace('""Tırnak""', "")


def test_csv_respects_all_filters(client, seeded):
    base = client.get("/sirketler.csv").text
    assert "Alfa" in base and "Beta" in base and "Gama" in base
    t = client.get("/sirketler.csv?status=sent").text
    assert "Gama" in t and "Alfa" not in t and "Beta" not in t
    t = client.get("/sirketler.csv?review=pending").text
    assert "Beta" in t and "Alfa" not in t
    t = client.get("/sirketler.csv?source=teknopark").text
    assert "Alfa" in t and "Beta" not in t
    t = client.get("/sirketler.csv?min_score=40").text
    assert "Beta" in t and "Alfa" not in t
    t = client.get("/sirketler.csv?q=gama&city=%C4%B0zmir").text
    assert "Gama" in t and "Alfa" not in t
    t = client.get("/sirketler.csv?q=gama&city=Konya").text  # filtreler VE ile birleşir
    assert "Gama" not in t and "Alfa" not in t
    assert client.get("/sirketler.csv?min_score=&page=abc").status_code == 200


# --- filtreler + sayfalama ------------------------------------------------------------------------------------------

def test_filters_combine_and_pagination_keeps_filters(client, engine):
    with _S(engine)() as s:
        for i in range(55):
            s.add(Company(name=f"Konya Firma {i:02d}", city="Konya", status=AppStatus.found, review_status=ReviewStatus.approved, score=i))
        for i in range(7):
            s.add(Company(name=f"Ankara Firma {i}", city="Ankara", status=AppStatus.found, review_status=ReviewStatus.approved, score=99))
        s.add(Company(name="Konya Gönderildi", city="Konya", status=AppStatus.sent, review_status=ReviewStatus.approved, score=100))
        s.commit()
    r = client.get("/sirketler?city=Konya&status=found&review=approved&sort=name")
    assert r.status_code == 200 and "(55)" in r.text and "Ankara Firma" not in r.text and "Konya Gönderildi" not in r.text
    assert "Sayfa 1 / 2" in r.text
    nxt = re.search(r'href="(/sirketler\?[^"]*page=2)"', r.text)
    assert nxt, "sonraki sayfa bağlantısı yok"
    link = nxt.group(1).replace("&amp;", "&")
    assert "city=Konya" in link and "status=found" in link and "review=approved" in link and "sort=name" in link
    r2 = client.get(link)
    assert r2.status_code == 200 and "Sayfa 2 / 2" in r2.text and "Konya Firma 54" in r2.text and "Konya Firma 00" not in r2.text
    assert 'value="Konya" selected' in r2.text  # filtre alanları seçili kalır
    csv_link = re.search(r'href="(/sirketler\.csv\?[^"]*)"', r2.text).group(1).replace("&amp;", "&")
    assert "city=Konya" in csv_link and "status=found" in csv_link and "page=" not in csv_link
    assert client.get(csv_link).text.count("Konya Firma") == 55
    # sayfa dışı
    r3 = client.get("/sirketler?city=Konya&status=found&page=9")
    assert r3.status_code == 200 and "Kayıt yok" in r3.text


def test_search_matches_notes_domain_sector(client, seeded, engine):
    with _S(engine)() as s:
        s.get(Company, seeded["b"]).notes = "çok özel anahtar"
        s.commit()
    assert "Beta" in client.get("/sirketler?q=%C3%B6zel+anahtar").text
    t = client.get("/sirketler?q=alfa.com").text
    assert "Alfa" in t and "Beta" not in t
    t = client.get("/sirketler?q=fintech").text
    assert "Alfa" in t and "Gama" not in t


# --- inceleme (HTMX) -------------------------------------------------------------------------------------------------

def test_review_htmx_fragment_updates_counter(client, engine):
    with _S(engine)() as s:
        ids = []
        for i in range(3):
            c = Company(name=f"Bekleyen {i}", city="Konya", review_status=ReviewStatus.pending, score=50 - i)
            s.add(c)
            s.flush()
            ids.append(c.id)
        services.add_contact(s, s.get(Company, ids[0]), "ik@bekleyen0.com", source="t")
        s.commit()
    page = client.get("/inceleme").text
    assert 'id="review-count"' in page and "(3 bekleyen)" in page
    r = client.post(f"/inceleme/{ids[0]}/onayla", headers=HX)
    assert r.status_code == 200 and _is_fragment(r.text)
    assert f'id="card-{ids[0]}"' in r.text and "Onaylandı" in r.text and "Hazır" in r.text
    assert 'hx-swap-oob="true"' in r.text and "(2 bekleyen)" in r.text
    r = client.post(f"/inceleme/{ids[1]}/sonra", headers=HX)
    assert "Sonraya" in r.text and "(1 bekleyen)" in r.text
    r = client.post(f"/inceleme/{ids[2]}/reddet", headers=HX)
    assert "Reddedildi" in r.text and "Kapandı" in r.text and "(0 bekleyen)" in r.text
    with _S(engine)() as s:
        assert s.get(Company, ids[0]).status == AppStatus.ready
        assert s.get(Company, ids[1]).review_status == ReviewStatus.later
        assert s.get(Company, ids[2]).status == AppStatus.closed
        assert any(e.type == EventType.manual for e in s.get(Company, ids[1]).events)
    assert "Bekleyen şirket yok" in client.get("/inceleme").text
    # HTMX olmayan istek yönlendirir
    assert client.post(f"/inceleme/{ids[0]}/onayla", follow_redirects=False).status_code == 303


def test_review_only_email_filter(client, seeded):
    t = client.get("/inceleme?only_email=true").text
    assert "Beta" not in t and "(0 bekleyen)" in t
    t = client.get("/inceleme?only_email=").text
    assert "Beta" in t


# --- mailler -----------------------------------------------------------------------------------------------------------

@pytest.fixture()
def fake_sender(monkeypatch):
    calls: list[dict] = []

    def fake_send_approved(session, *, dry_run=False, force=False, limit=None, **kw):
        calls.append({"dry_run": dry_run, "force": force, "limit": limit})
        return mail_sender.SendReport(sent=1, skipped=2, failed=0, mode="dry", dry_run=dry_run, reason="sahte")

    monkeypatch.setattr(mail_sender, "send_approved", fake_send_approved)
    return calls


def test_send_button_defaults_to_dry_run(client, seeded, engine, fake_sender):
    with _S(engine)() as s:
        s.add(EmailMessage(company_id=seeded["a"], direction=Direction.outbound, status=MailStatus.approved, to_addr="kariyer@alfa.com.tr", subject="s", body_text="b"))
        s.commit()
    page = client.get("/mailler?tab=approved").text
    m = re.search(r'<select name="dry_run".*?</select>', page, re.S)
    assert m, "dry_run seçimi yok"
    assert re.search(r'<option value="true"[^>]*selected', m.group(0)), "prova varsayılan seçili değil"
    assert 'name="dry_run" value="true" checked' in page or "selected" in m.group(0)
    # Alan hiç gönderilmezse (eski form / curl) -> prova
    r = client.post("/mailler/gonder", data={}, headers=HX)
    assert r.status_code == 200 and _is_fragment(r.text) and "prova" in r.text
    assert fake_sender[-1]["dry_run"] is True
    r = client.post("/mailler/gonder", data={"dry_run": "true", "force": "true", "limit": ""}, headers=HX)
    assert fake_sender[-1] == {"dry_run": True, "force": True, "limit": None}
    # sonuç satırı sayfada görünür
    page = client.get("/mailler?tab=approved").text
    assert "Son gönderim" in page and "(prova)" in page and "gönderilen 1" in page and "atlanan 2" in page and "sahte" in page
    # yalnızca açıkça false -> gerçek gönderim (mail_mode=dry olduğu için kimlik bilgisi gerekmez)
    r = client.post("/mailler/gonder", data={"dry_run": "false", "limit": "3"}, follow_redirects=True)
    assert fake_sender[-1] == {"dry_run": False, "force": False, "limit": 3}
    assert "prova" not in r.text.split("Son gönderim")[0].split("başladı")[1][:40]


def test_real_send_without_credentials_refused(client, seeded, fake_sender, monkeypatch):
    monkeypatch.setattr(get_settings(), "mail_mode", "send")
    r = client.post("/mailler/gonder", data={"dry_run": "false"}, headers=HX)
    assert r.status_code == 200 and "MAIL_USER" in r.text and fake_sender == []
    # prova her zaman çalışır
    client.post("/mailler/gonder", data={}, headers=HX)
    assert fake_sender and fake_sender[-1]["dry_run"] is True


def test_approve_saves_pending_edits_and_requires_recipient(client, seeded, engine):
    with _S(engine)() as s:
        d = EmailMessage(company_id=seeded["a"], direction=Direction.outbound, status=MailStatus.draft, to_addr="kariyer@alfa.com.tr", subject="Eski [BV-0001]", body_text="eski", tracking_code="BV-0001")
        no_to = EmailMessage(company_id=seeded["a"], direction=Direction.outbound, status=MailStatus.draft, to_addr=None, subject="x", body_text="y")
        s.add_all([d, no_to])
        s.commit()
        did, nid = d.id, no_to.id
    r = client.post(f"/mailler/{did}/onayla", data={"subject": "Yeni konu", "body_text": "yeni gövde\r\nBV-0001"}, headers=HX)
    assert r.status_code == 200 and "Onaylandı" in r.text
    with _S(engine)() as s:
        m = s.get(EmailMessage, did)
        assert m.status == MailStatus.approved and m.subject == "Yeni konu" and m.body_text == "yeni gövde\nBV-0001\n"
    r = client.post(f"/mailler/{nid}/onayla")
    assert r.status_code == 400 and "Alıcı" in r.text
    with _S(engine)() as s:
        assert s.get(EmailMessage, nid).status == MailStatus.draft


def test_edit_warns_when_tracking_code_removed_and_rejects_empty_body(client, seeded, engine):
    with _S(engine)() as s:
        d = EmailMessage(company_id=seeded["a"], direction=Direction.outbound, status=MailStatus.draft, to_addr="k@alfa.com.tr", subject="Staj [BV-0001]", body_text="gövde BV-0001", tracking_code="BV-0001")
        s.add(d)
        s.commit()
        did = d.id
    r = client.post(f"/mailler/{did}/duzenle", data={"subject": "Kodsuz", "body_text": "kodsuz gövde"}, headers=HX)
    assert r.status_code == 200 and "Kaydedildi" in r.text and "takip kodu BV-0001" in r.text
    r = client.post(f"/mailler/{did}/duzenle", data={"subject": "Kodsuz", "body_text": "   "}, headers=HX)
    assert r.status_code == 200 and "boş olamaz" in r.text
    with _S(engine)() as s:
        assert s.get(EmailMessage, did).body_text == "kodsuz gövde\n"


def test_mail_delete_rules_and_inbound_isolation(client, seeded, engine):
    with _S(engine)() as s:
        failed = EmailMessage(company_id=seeded["a"], direction=Direction.outbound, status=MailStatus.failed, to_addr="k@alfa.com.tr", subject="f", body_text="f", error="SMTP 550")
        s.add(failed)
        s.commit()
        fid = failed.id
    # gönderilmiş silinemez / düzenlenemez / yeniden üretilemez
    out = seeded["out"]
    assert client.post(f"/mailler/{out}/sil").status_code == 400
    assert client.post(f"/mailler/{out}/duzenle", data={"subject": "a", "body_text": "b"}).status_code == 400
    assert client.post(f"/mailler/{out}/yeniden-uret").status_code == 400
    assert client.post(f"/mailler/{out}/geri-al").status_code == 400
    # gelen mesaj mail rotalarında yok sayılır (404)
    inb = seeded["inb"]
    for action in ("sil", "onayla", "geri-al", "yeniden-uret"):
        assert client.post(f"/mailler/{inb}/{action}").status_code == 404, action
    # hatalı mesaj taslağa döner ya da silinir
    r = client.post(f"/mailler/{fid}/geri-al", headers=HX)
    assert r.status_code == 200 and "Taslağa" in r.text
    with _S(engine)() as s:
        m = s.get(EmailMessage, fid)
        assert m.status == MailStatus.draft and m.error is None
    assert client.post(f"/mailler/{fid}/sil", headers=HX).text == ""
    with _S(engine)() as s:
        assert s.get(EmailMessage, fid) is None and s.get(EmailMessage, out) is not None and s.get(EmailMessage, inb) is not None


def test_regenerate_without_llm_overwrites_same_draft(client, seeded, engine):
    a = seeded["a"]
    client.post(f"/sirketler/{a}/taslak", data={})
    with _S(engine)() as s:
        m = s.scalar(select(EmailMessage).where(EmailMessage.company_id == a))
        mid = m.id
    client.post(f"/mailler/{mid}/duzenle", data={"subject": "Elle", "body_text": "elle gövde"})
    r = client.post(f"/mailler/{mid}/yeniden-uret", data={"use_llm": "false"}, headers=HX)
    assert r.status_code == 200 and "şablon paragrafı" in r.text and f'id="mail-{mid}"' in r.text
    with _S(engine)() as s:
        msgs = list(s.scalars(select(EmailMessage).where(EmailMessage.company_id == a)))
        assert len(msgs) == 1 and msgs[0].id == mid and msgs[0].subject != "Elle" and "BV-" in msgs[0].subject


# --- gelen kutusu -----------------------------------------------------------------------------------------------------

def test_inbox_bind_sets_replied_via_services(client, seeded, engine):
    c = seeded["c"]  # status: sent
    r = client.post(f"/gelen/{seeded['orphan']}/bagla", data={"company_id": c}, follow_redirects=True)
    assert r.status_code == 200 and "bağlandı" in r.text
    with _S(engine)() as s:
        comp = s.get(Company, c)
        assert comp.status == AppStatus.replied
        assert any(e.type == EventType.email_received and e.email_message_id == seeded["orphan"] for e in comp.events)
        assert any(e.type == EventType.status_change and e.new_status == AppStatus.replied for e in comp.events)
    assert client.get("/gelen?tab=unmatched").text.count("reklam") == 0
    assert client.post(f"/gelen/{seeded['orphan']}/bagla", data={"company_id": 9999}).status_code == 404
    assert client.post(f"/gelen/{seeded['out']}/duygu", data={"sentiment": "positive"}).status_code == 404


def test_sentiment_confirm_uses_services_and_turkish_labels(client, seeded, engine):
    inb, c = seeded["inb"], seeded["c"]
    r = client.post(f"/gelen/{inb}/duygu", data={"sentiment": "negative", "apply_status": "true"}, follow_redirects=True)
    assert r.status_code == 200 and "Olumsuz" in r.text and "'negative'" not in r.text
    with _S(engine)() as s:
        comp = s.get(Company, c)
        assert comp.status == AppStatus.negative and s.get(EmailMessage, inb).sentiment == Sentiment.negative
        ev = [e for e in comp.events if e.type == EventType.status_change and e.new_status == AppStatus.negative]
        assert len(ev) == 1 and ev[0].created_by == Actor.user and ev[0].email_message_id == inb
        assert "negative" not in ev[0].title and "Olumsuz" in ev[0].title
        n_before = len(comp.events)
    # aynı duruma tekrar onay yeni Event yazmaz
    client.post(f"/gelen/{inb}/duygu", data={"sentiment": "negative", "apply_status": "true"})
    with _S(engine)() as s:
        assert len(s.get(Company, c).events) == n_before
    # apply_status yoksa yalnızca tahmin değişir
    client.post(f"/gelen/{inb}/duygu", data={"sentiment": "info"})
    with _S(engine)() as s:
        assert s.get(EmailMessage, inb).sentiment == Sentiment.info and s.get(Company, c).status == AppStatus.negative
    # sahipsiz mesajda apply_status durum değiştirmez ama 500 vermez
    r = client.post(f"/gelen/{seeded['orphan']}/duygu", data={"sentiment": "positive", "apply_status": "true"}, follow_redirects=True)
    assert r.status_code == 200 and "bağlı değil" in r.text


def test_dashboard_unmatched_count_matches_inbox_tab(client, seeded, engine):
    with _S(engine)() as s:
        # şirkete bağlı ama matched_by=none kalmış tutarsız kayıt: sahipsiz sayılmamalı
        s.add(EmailMessage(company_id=seeded["c"], direction=Direction.inbound, status=MailStatus.received, from_addr="z@gama.com", subject="z", body_text="z", matched_by=MatchMethod.none))
        s.commit()
    inbox = client.get("/gelen?tab=unmatched").text
    assert "Sahipsiz (1)" in inbox
    dash = client.get("/").text
    m = re.search(r"Sahipsiz gelen mail</small><h3>(\d+)</h3>", dash)
    assert m and m.group(1) == "1"


# --- ayarlar ----------------------------------------------------------------------------------------------------------

def test_settings_masks_all_secrets(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", "sk-ant-cok-gizli-ANAHTAR-9876")
    monkeypatch.setattr(s, "google_places_api_key", "AIzaGIZLI-places-5555")
    monkeypatch.setattr(s, "mail_password", "uygulama sifresi 4321")
    t = client.get("/ayarlar").text
    for leak in ("cok-gizli", "GIZLI-places", "uygulama sifresi", "sk-ant-cok"):
        assert leak not in t
    assert "9876" in t and "5555" in t and "4321" in t and "••••" in t
    assert "ANTHROPIC_API_KEY" in t and "MAIL_PASSWORD" in t and "GOOGLE_PLACES_API_KEY" in t


def test_profile_save_only_to_settings_path(client, tmp_path):
    target = Path(get_settings().abs_profile_path)
    assert target == tmp_path / "profil.md"
    r = client.post("/ayarlar/profil", data={"text": "# Profil\r\nÇğıöşü İI\r\n", "path": str(tmp_path / "baska.md"), "profile_path": "../../etc/x"}, follow_redirects=True)
    assert r.status_code == 200 and "Profil kaydedildi" in r.text
    raw = target.read_bytes()
    assert raw.decode("utf-8").replace("\r\n", "\n") == "# Profil\nÇğıöşü İI\n"  # UTF-8 (BOM yok); satır sonu platforma göre
    assert "Çğıöşü İI".encode("utf-8") in raw and not raw.startswith(b"\xef\xbb\xbf")
    assert not (tmp_path / "baska.md").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["profil.md"]
    # boş profil yazılmaz
    r = client.post("/ayarlar/profil", data={"text": "   \r\n  "}, follow_redirects=True)
    assert "boş olamaz" in r.text and target.read_text(encoding="utf-8") == "# Profil\nÇğıöşü İI\n"
    t = client.get("/ayarlar").text
    assert "Çğıöşü İI" in t and "<script" not in t.replace("<script src=", "")


def test_settings_page_without_profile_shows_example(client, tmp_path):
    t = client.get("/ayarlar").text
    assert "Profil dosyası yok" in t and "<textarea" in t


# --- genel: her sayfada mail modu, Türkçe, CDN sürümleri, sınırlı sorgular ------------------------------------------

@pytest.mark.parametrize("path", ["/", "/sirketler", "/inceleme", "/mailler", "/gelen", "/istatistik", "/ayarlar", "/yok-404"])
def test_mail_mode_badge_on_every_page(client, seeded, path):
    t = client.get(path).text
    assert "mail modu" in t and "<strong>dry</strong>" in t


def test_mail_mode_badge_reflects_setting(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mail_mode", "draft")
    assert "<strong>draft</strong>" in client.get("/").text


ENGLISH_WORDS = re.compile(r"(?<![\w/.-])(Not Found|Submit|Delete|Save|Search|Loading|Error|Cancel|Settings|Companies|Approve|Reject|Dashboard|Inbox|Statistics|Page \d|Next|Previous|Unknown|None)(?![\w-])")


def test_templates_have_no_english_leftovers():
    for tpl in TEMPLATES_DIR.glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        # Jinja ifadeleri ve HTML öznitelikleri dışındaki görünür metin
        visible = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|<[^>]+>", " ", text)
        hits = ENGLISH_WORDS.findall(visible)
        assert not hits, (tpl.name, hits)


def test_rendered_pages_have_no_raw_enum_values(client, seeded):
    a = seeded["a"]
    client.post(f"/sirketler/{a}/durum", data={"status": "interview", "note": "n"})
    t = client.get(f"/sirketler/{a}").text
    for raw in (">status_change<", ">manual<", ">note<", ">user<", ">system<", ">approved<", ">pending<"):
        assert raw not in t, raw
    t = client.get("/gelen").text
    assert ">positive<" not in t and ">domain<" not in t


def test_cdn_assets_pin_exact_versions():
    base = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    urls = re.findall(r'(?:href|src)="(https://[^"]+)"', base)
    assert urls, "CDN bağlantısı yok"
    for u in urls:
        assert re.search(r"@\d+\.\d+\.\d+/", u), f"sürüm sabitlenmemiş: {u}"
        assert "@latest" not in u and not re.search(r"@\d+(\.\d+)?/", u)


def test_dashboard_lists_are_bounded(client, engine):
    with _S(engine)() as s:
        now = datetime.utcnow()
        for i in range(30):
            c = Company(name=f"Takip {i}", city="Konya", status=AppStatus.sent, review_status=ReviewStatus.approved, next_followup_at=now + timedelta(hours=i))
            s.add(c)
            s.flush()
            for j in range(3):
                s.add(Event(company=c, type=EventType.note, title=f"Not {i}-{j}"))
        s.commit()
    t = client.get("/").text
    upcoming = t.split("Yaklaşan hatırlatmalar")[1].split("</article>")[0]
    assert upcoming.count("<li>") == 10
    recent = t.split("Son hareketler")[1].split("</table>")[0]
    assert recent.count("<tr>") == 12
    assert "Takip" in t


def test_list_pages_are_bounded(client, engine):
    with _S(engine)() as s:
        for i in range(60):
            s.add(Company(name=f"Pend {i}", city="Konya", review_status=ReviewStatus.pending))
        s.commit()
    t = client.get("/inceleme").text
    assert t.count('class="card" id="card-') == 40 and "(60 bekleyen)" in t
    t = client.get("/sirketler").text
    assert t.count('<a href="/sirketler/') <= 50 + 5 and "(60)" in t


def test_blacklisted_company_hidden_from_review_queue(client, seeded, engine):
    b = seeded["b"]
    client.post(f"/sirketler/{b}/karaliste", data={"flag": "true"})
    assert "Beta" not in client.get("/inceleme").text
    with _S(engine)() as s:
        c = s.get(Company, b)
        assert c.blacklisted and c.status == AppStatus.closed
    # kara listedeki şirket için taslak üretilmez (500 değil, Türkçe mesaj)
    r = client.post(f"/sirketler/{b}/taslak", follow_redirects=True)
    assert r.status_code == 200 and "Taslak üretilemedi" in r.text


def test_add_contact_make_primary_and_duplicate(client, seeded, engine):
    a = seeded["a"]
    client.post(f"/sirketler/{a}/kisi", data={"email": "INFO@Alfa.com.tr", "make_primary": "true"})
    with _S(engine)() as s:
        c = s.get(Company, a)
        emails = {k.email: k.is_primary for k in c.contacts}
        assert emails == {"kariyer@alfa.com.tr": False, "info@alfa.com.tr": True}
    r = client.post(f"/sirketler/{a}/kisi", data={"email": "info@alfa.com.tr"}, follow_redirects=True)
    assert r.status_code == 200
    with _S(engine)() as s:
        assert len(s.get(Company, a).contacts) == 2
    r = client.post(f"/sirketler/{a}/kisi", data={"email": "<script>@x.com"}, follow_redirects=True)
    assert "Geçersiz e-posta" in r.text and "<script>" not in r.text
