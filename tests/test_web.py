"""web/ testleri: tüm sayfalar 200, HTMX ve form işlemleri, 404."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from stajbot import db, services
from stajbot.config import get_settings
from stajbot.enums import AppStatus, Direction, MailStatus, MatchMethod, ReviewStatus, Sentiment
from stajbot.models import Company, CompanySource, EmailMessage
from stajbot.web.app import app
from stajbot.web.deps import get_db


@pytest.fixture()
def client(engine, monkeypatch, tmp_path):
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def _db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_sessionmaker", lambda eng=None: factory if eng is None else sessionmaker(bind=eng, expire_on_commit=False, future=True))
    monkeypatch.setattr(get_settings(), "profile_path", tmp_path / "profil.md")
    monkeypatch.setattr(get_settings(), "mail_mode", "dry")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded(engine):
    S = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with S() as s:
        a = Company(name="Alfa Yazılım A.Ş.", domain="alfa.com.tr", website="https://alfa.com.tr", city="Konya", sector="Fintech",
                    tech_stack=["Python"], review_status=ReviewStatus.approved, is_teknopark=True, description="Ödeme sistemleri")
        b = Company(name="Beta Bilişim", city="Ankara", review_status=ReviewStatus.pending, score=42, description="x" * 300)
        c = Company(name="Gama", city="İzmir", status=AppStatus.sent, review_status=ReviewStatus.approved, last_contact_at=datetime(2026, 10, 1), next_followup_at=datetime(2026, 10, 11))
        s.add_all([a, b, c])
        s.flush()
        s.add(CompanySource(company=a, source="teknopark", raw={"k": "v"}))
        s.add(CompanySource(company=b, source="argem", raw={}))
        services.add_contact(s, a, "kariyer@alfa.com.tr", source="t")
        services.add_contact(s, c, "info@gama.com", source="t")
        out = EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.sent, message_id="<o1@x>", subject=f"Staj [{c.tracking_code}]", to_addr="info@gama.com", sent_at=datetime(2026, 10, 1), body_text="m")
        inb = EmailMessage(company=c, direction=Direction.inbound, status=MailStatus.received, from_addr="info@gama.com", subject="Re: Staj", body_text="Mülakata davet", matched_by=MatchMethod.domain, sentiment=Sentiment.positive, received_at=datetime(2026, 10, 3))
        orphan = EmailMessage(direction=Direction.inbound, status=MailStatus.received, from_addr="x@gmail.com", subject="reklam", body_text="...", matched_by=MatchMethod.none)
        s.add_all([out, inb, orphan])
        s.commit()
        return {"a": a.id, "b": b.id, "c": c.id, "inb": inb.id, "orphan": orphan.id, "out": out.id}


PAGES = ["/", "/sirketler", "/inceleme", "/mailler", "/gelen", "/istatistik", "/ayarlar", "/sirketler.csv",
         "/mailler?tab=sent", "/mailler?tab=approved", "/gelen?tab=unmatched", "/sirketler?q=alfa&city=Konya&status=found&review=approved&size=unknown&source=teknopark&min_score=0&sort=name",
         "/inceleme?city=Ankara&only_email=true", "/sirketler?page=5"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_200(client, seeded, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.text[:300])


@pytest.mark.parametrize("path", ["/", "/sirketler", "/inceleme", "/mailler", "/gelen", "/istatistik", "/ayarlar", "/sirketler.csv"])
def test_pages_200_empty_db(client, path):
    assert client.get(path).status_code == 200


def test_company_detail_and_404(client, seeded):
    r = client.get(f"/sirketler/{seeded['a']}")
    assert r.status_code == 200 and "Alfa Yazılım" in r.text and "kariyer@alfa.com.tr" in r.text and "BV-" in r.text
    r404 = client.get("/sirketler/9999")
    assert r404.status_code == 404 and "bulunamadı" in r404.text
    assert client.get("/sirketler/9999", headers={"HX-Request": "true"}).status_code == 404


def test_csv_export(client, seeded):
    r = client.get("/sirketler.csv?city=Konya")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert "Alfa" in r.text and "Beta" not in r.text


def test_review_htmx_actions(client, seeded, engine):
    b = seeded["b"]
    r = client.post(f"/inceleme/{b}/onayla", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Onaylandı" in r.text and f"card-{b}" in r.text
    S = sessionmaker(bind=engine)
    with S() as s:
        assert s.get(Company, b).review_status == ReviewStatus.approved
    r2 = client.post(f"/inceleme/{b}/reddet", follow_redirects=False)
    assert r2.status_code == 303
    with S() as s:
        c = s.get(Company, b)
        assert c.review_status == ReviewStatus.rejected and c.status == AppStatus.closed
    assert client.post(f"/inceleme/{b}/yanlis").status_code == 400
    assert client.post("/inceleme/9999/onayla").status_code == 404


def test_company_actions(client, seeded, engine):
    a = seeded["a"]
    S = sessionmaker(bind=engine)
    r = client.post(f"/sirketler/{a}/durum", data={"status": "interview", "note": "12 Mart"}, follow_redirects=True)
    assert r.status_code == 200 and "Durum güncellendi" in r.text
    client.post(f"/sirketler/{a}/not", data={"body": "ilk not"})
    client.post(f"/sirketler/{a}/kisi", data={"email": "ayse.y@alfa.com.tr", "name": "Ayşe", "kind": "personal"})
    assert "Geçersiz e-posta" in client.post(f"/sirketler/{a}/kisi", data={"email": "bozuk"}, follow_redirects=True).text
    with S() as s:
        c = s.get(Company, a)
        assert c.status == AppStatus.interview
        assert any(e.body == "ilk not" for e in c.events)
        emails = {k.email: k for k in c.contacts}
        assert "ayse.y@alfa.com.tr" in emails and emails["kariyer@alfa.com.tr"].is_primary
        cid = emails["ayse.y@alfa.com.tr"].id
    client.post(f"/sirketler/{a}/kisi/{cid}/birincil")
    with S() as s:
        assert s.get(Company, a).primary_contact.email == "ayse.y@alfa.com.tr"
    client.post(f"/sirketler/{a}/kisi/{cid}/sil")
    with S() as s:
        c = s.get(Company, a)
        assert [k.email for k in c.contacts] == ["kariyer@alfa.com.tr"] and c.primary_contact is not None
    r = client.post(f"/sirketler/{a}/alanlar", data={"website": "alfa.com.tr", "employee_count": "120", "linkedin_url": "https://linkedin.com/company/alfa", "career_url": "", "sector": "Fintech", "priority": "9", "school_protocol": "true", "notes": "önemli"}, follow_redirects=True)
    assert r.status_code == 200
    with S() as s:
        c = s.get(Company, a)
        assert c.employee_count == 120 and c.priority == 5 and c.school_protocol and c.notes == "önemli" and c.size_bucket.value == "s51_200"
    client.post(f"/sirketler/{a}/puanla")
    with S() as s:
        assert s.get(Company, a).score > 0
    client.post(f"/sirketler/{a}/karaliste", data={"flag": "true"})
    with S() as s:
        c = s.get(Company, a)
        assert c.blacklisted and c.status == AppStatus.closed
    client.post(f"/sirketler/{a}/karaliste", data={"flag": "false"})
    with S() as s:
        assert not s.get(Company, a).blacklisted
    assert client.post(f"/sirketler/{a}/durum", data={"status": "yok"}).status_code == 400
    assert client.post(f"/sirketler/{a}/inceleme", data={"action": "later"}, follow_redirects=True).status_code == 200


def test_compose_approve_edit_delete_flow(client, seeded, engine):
    a = seeded["a"]
    S = sessionmaker(bind=engine)
    r = client.post(f"/sirketler/{a}/taslak", data={}, follow_redirects=True)
    assert r.status_code == 200 and "Taslak üretildi" in r.text
    with S() as s:
        m = s.scalar(select(EmailMessage).where(EmailMessage.company_id == a))
        assert m.status == MailStatus.draft and s.get(Company, a).status == AppStatus.drafted
        mid = m.id
    r = client.post(f"/mailler/{mid}/duzenle", data={"subject": "Yeni konu [BV-0001]", "body_text": "gövde\r\nsatır"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Kaydedildi" in r.text
    r = client.post(f"/mailler/{mid}/yeniden-uret", data={"use_llm": "false"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Yeniden üretildi" in r.text
    r = client.post(f"/mailler/{mid}/onayla", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Onaylandı" in r.text
    with S() as s:
        assert s.get(EmailMessage, mid).status == MailStatus.approved and s.get(Company, a).status == AppStatus.queued
    assert client.post(f"/mailler/{mid}/onayla").status_code == 400
    assert client.get("/mailler?tab=approved").status_code == 200
    # prova gönderim (arka plan görevi TestClient'ta eşzamanlı çalışır)
    r = client.post("/mailler/gonder", data={"dry_run": "true", "force": "true"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "başladı" in r.text
    with S() as s:
        assert s.get(EmailMessage, mid).status == MailStatus.approved  # prova: değişmez
    r = client.post(f"/mailler/{mid}/geri-al", headers={"HX-Request": "true"})
    assert r.status_code == 200
    with S() as s:
        assert s.get(EmailMessage, mid).status == MailStatus.draft and s.get(Company, a).status == AppStatus.drafted
    r = client.post(f"/mailler/{mid}/sil", headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.text == ""
    with S() as s:
        assert s.get(EmailMessage, mid) is None and s.get(Company, a).status == AppStatus.ready
    assert client.post("/mailler/9999/onayla").status_code == 404
    assert client.post(f"/mailler/{seeded['out']}/sil").status_code == 400  # gönderilmiş silinmez


def test_compose_without_contact_shows_error(client, seeded):
    r = client.post(f"/sirketler/{seeded['b']}/taslak", follow_redirects=True)
    assert r.status_code == 200 and "Taslak üretilemedi" in r.text


def test_inbox_actions(client, seeded, engine):
    S = sessionmaker(bind=engine)
    r = client.post(f"/gelen/{seeded['orphan']}/bagla", data={"company_id": seeded["a"]}, follow_redirects=True)
    assert r.status_code == 200 and "bağlandı" in r.text
    with S() as s:
        m = s.get(EmailMessage, seeded["orphan"])
        assert m.company_id == seeded["a"] and m.matched_by == MatchMethod.manual
    r = client.post(f"/gelen/{seeded['inb']}/duygu", data={"sentiment": "positive", "apply_status": "true"}, follow_redirects=True)
    assert r.status_code == 200
    with S() as s:
        assert s.get(Company, seeded["c"]).status == AppStatus.positive
    assert client.post(f"/gelen/{seeded['inb']}/duygu", data={"sentiment": "zzz"}).status_code == 400
    assert client.post(f"/gelen/{seeded['out']}/bagla", data={"company_id": seeded["a"]}).status_code == 404
    r = client.post("/gelen/oku", follow_redirects=True)
    assert r.status_code == 200 and "MAIL_USER" in r.text


def test_settings_profile_save(client, tmp_path):
    r = client.get("/ayarlar")
    assert r.status_code == 200 and "Profil" in r.text and "ANTHROPIC_API_KEY" in r.text
    r = client.post("/ayarlar/profil", data={"text": "# Ben\r\nPython"}, follow_redirects=True)
    assert r.status_code == 200 and "Profil kaydedildi" in r.text
    assert (tmp_path / "profil.md").read_text(encoding="utf-8") == "# Ben\nPython\n"
    assert "# Ben" in client.get("/ayarlar").text


def test_secrets_masked(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-ant-secret-value-1234")
    text = client.get("/ayarlar").text
    assert "sk-ant-secret" not in text and "1234" in text


def test_static_css(client):
    assert client.get("/static/style.css").status_code == 200


def test_no_print_in_web():
    import inspect

    from stajbot.web import app as app_mod, deps
    from stajbot.web.routes import companies, inbox, mail, review, settings_page, stats

    for mod in (app_mod, deps, companies, inbox, mail, review, settings_page, stats):
        assert "print(" not in inspect.getsource(mod)
