"""enrich/ testleri: keywords, scoring, website (respx ile sahte site)."""
from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import select

from stajbot import services
from stajbot.enrich import keywords, scoring, website
from stajbot.enums import AppStatus, ContactKind, EventType, ReviewStatus, SizeBucket
from stajbot.models import Company, CompanySource, Event

# --- keywords ---------------------------------------------------------------------

def test_detect_technologies_turkish_and_english():
    text = "Python ve Django ile backend, React ile ön yüz geliştiriyoruz. Yapay zekâ ve makine öğrenmesi projeleri. AWS üzerinde Kubernetes."
    techs = keywords.detect_technologies(text)
    for t in ("Python", "React", "Yapay zeka", "Makine öğrenmesi", "AWS", "Kubernetes"):
        assert t in techs
    assert "Java" not in techs


def test_java_vs_javascript_and_go():
    assert "Java" not in keywords.detect_technologies("JavaScript ve TypeScript")
    assert "Java" in keywords.detect_technologies("Java Spring Boot")
    assert "Go" not in keywords.detect_technologies("let's go to the office")
    assert "Go" in keywords.detect_technologies("Golang mikroservisler")
    assert "C#" in keywords.detect_technologies("C# ve .NET Core")
    assert ".NET" in keywords.detect_technologies("ASP.NET uygulamaları")


def test_detect_sector_prefers_specific():
    assert keywords.detect_sector("Savunma sanayi için radar yazılımı geliştiren bir yazılım şirketiyiz") == "Savunma"
    assert keywords.detect_sector("yazılım şirketi") == "Yazılım"
    assert keywords.detect_sector("") is None
    assert keywords.detect_sector("ödeme sistemleri ve sanal POS çözümleri, fintech") == "Fintech"


def test_empty_text():
    assert keywords.detect_technologies(None) == []
    assert keywords.detect_technologies("") == []


# --- scoring ----------------------------------------------------------------------

def _company(session, **kw) -> Company:
    base = dict(name="X", city="Konya")
    base.update(kw)
    c = Company(**base)
    session.add(c)
    session.flush()
    return c


def test_score_zero_outside_target(session):
    c = _company(session, city="Trabzon", is_arge_center=True, school_protocol=True)
    assert scoring.compute_score(c, []) == 0
    assert "Hedef şehir dışı" in scoring.explain(c, [])[0]


def test_score_weights(session):
    c = _company(session, is_arge_center=True, is_teknopark=True, school_protocol=True, has_internship_posting=True,
                 tech_stack=["Python", "React", "AWS", "Docker", "Go"], employee_count=50, size_bucket=SizeBucket.s11_50)
    session.add_all([CompanySource(company=c, source="argem"), CompanySource(company=c, source="teknopark")])
    services.add_contact(session, c, "kariyer@x.com", source="t")
    session.flush()
    # 10 + 25 + 15 + 10 + 25 + 15 + 20 (5 teknoloji, tavan 20) + 5 = 125 -> 100
    assert scoring.compute_score(c, ["python", "React", "aws", "docker", "go"]) == 100
    c2 = _company(session, name="Y", tech_stack=["Python"])
    services.add_contact(session, c2, "info@y.com", source="t")
    assert scoring.compute_score(c2, ["Python"]) == 5 + 5
    assert scoring.compute_score(c2, None) in (5, 10)  # profil dosyası olabilir de olmayabilir


def test_score_hr_vs_info(session):
    c = _company(session)
    services.add_contact(session, c, "info@x.com", source="t")
    assert scoring.compute_score(c, []) == 5
    services.add_contact(session, c, "ik@x.com", source="t")
    assert scoring.compute_score(c, []) == 15
    assert any("İK" in line for line in scoring.explain(c, []))


def test_profile_techs_missing_file(tmp_path):
    assert scoring.profile_techs(tmp_path / "yok.md") == []
    p = tmp_path / "profil.md"
    p.write_text("Python, FastAPI ve React biliyorum; Docker kullanıyorum.", encoding="utf-8")
    assert set(scoring.profile_techs(p)) >= {"Python", "React", "Docker"}


# --- website: ayrıştırma -----------------------------------------------------------

def _cf(email: str, key: int = 0x8E) -> str:
    return bytes([key] + [b ^ key for b in email.encode()]).hex()


def test_extract_emails_variants():
    html = (
        '<a href="mailto:Info@Firma.com.tr">yaz</a> '
        f'<span class="__cf_email__" data-cfemail="{_cf("ik@firma.com")}">[email protected]</span> '
        'kariyer [at] firma [dot] com ; logo@2x.png ; noreply@firma.com.tr ; test@example.com ; x@site.png'
    )
    emails = website.extract_emails(html)
    assert "info@firma.com.tr" in emails
    assert "kariyer@firma.com" in emails
    assert "ik@firma.com" in emails
    assert not any(e.endswith(".png") for e in emails)
    assert "noreply@firma.com.tr" not in emails and "test@example.com" not in emails


def test_decode_cfemail_invalid():
    assert website.decode_cfemail("zz") is None and website.decode_cfemail("8e") is None


def test_find_employee_count():
    assert website.find_employee_count("500+ çalışanımızla hizmet veriyoruz") == 500
    assert website.find_employee_count("100'den fazla mühendis") == 100
    assert website.find_employee_count("3 ofis") is None
    assert website.find_employee_count("Türkiye'de 1500 kişilik ekip, Konya'da 40 mühendis") == 1500
    assert website.find_employee_count("over 250 employees") == 250


HOME = """<html><head><title>Alfa</title><meta name="description" content="Python ve React ile fintech çözümleri"></head>
<body><p>Alfa Yazılım: fintech, ödeme sistemleri ve sanal POS; Python, Kubernetes. 120+ çalışan.</p>
<a href="/iletisim">İletişim</a> <a href="/kariyer">Kariyer</a> <a href="https://www.linkedin.com/company/alfa-yazilim/">LinkedIn</a>
<a href="https://baska.com/x">dış</a> <a href="/hakkimizda">Hakkımızda</a><script src="/static/react.production.min.js"></script></body></html>"""
CONTACT = '<html><body>Bize yazın: <a href="mailto:info@alfa.com.tr">info@alfa.com.tr</a> ik (at) alfa (dot) com (dot) tr</body></html>'
CAREER = "<html><body><h1>Kariyer</h1><p>Stajyer mühendis arıyoruz. Java bilen.</p></body></html>"
ABOUT = "<html><body>2010'da kurulduk. Savunma sanayine de hizmet veriyoruz.</body></html>"


def _mock_site(respx_mock, robots: str | None = None, home_status: int = 200):
    respx_mock.get("https://alfa.com.tr/robots.txt").mock(return_value=httpx.Response(200, text=robots) if robots is not None else httpx.Response(404))
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(home_status, text=HOME, headers={"content-type": "text/html; charset=utf-8"}))
    respx_mock.get("https://alfa.com.tr/iletisim").mock(return_value=httpx.Response(200, text=CONTACT, headers={"content-type": "text/html"}))
    respx_mock.get("https://alfa.com.tr/kariyer").mock(return_value=httpx.Response(200, text=CAREER, headers={"content-type": "text/html"}))
    respx_mock.get("https://alfa.com.tr/hakkimizda").mock(return_value=httpx.Response(200, text=ABOUT, headers={"content-type": "text/html"}))


@pytest.fixture()
def client():
    with httpx.Client(follow_redirects=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(website.Throttle, "wait", lambda self: None)


@respx.mock(assert_all_called=False)
def test_crawl_collects_everything(respx_mock, client):
    _mock_site(respx_mock)
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.pages_fetched == 4
    assert set(res.emails) == {"info@alfa.com.tr", "ik@alfa.com.tr"}
    assert {"Python", "React", "Kubernetes", "Fintech", "Java"} <= set(res.tech_stack)
    assert res.career_url == "https://alfa.com.tr/kariyer" and res.has_internship_posting
    assert res.linkedin_url == "https://www.linkedin.com/company/alfa-yazilim"
    assert res.employee_count == 120
    assert res.sector == "Fintech"
    assert not any("baska.com" in str(c.request.url) for c in respx_mock.calls)


@respx.mock(assert_all_called=False)
def test_enrich_company_writes_db(respx_mock, client, session):
    _mock_site(respx_mock)
    c = Company(name="Alfa Yazılım", website="https://alfa.com.tr/", city="Konya", review_status=ReviewStatus.approved, sector="Mevcut Sektör")
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=client)
    assert res.ok and res.new_contacts == 2
    assert c.primary_contact.email == "ik@alfa.com.tr" and c.primary_contact.kind == ContactKind.hr
    assert c.sector == "Mevcut Sektör"  # dolu alan korunur
    assert c.career_url == "https://alfa.com.tr/kariyer" and c.has_internship_posting and c.internship_posting_url
    assert c.linkedin_url and c.employee_count == 120 and c.size_bucket == SizeBucket.s51_200
    assert "Python" in c.tech_stack and c.enriched_at is not None and c.domain == "alfa.com.tr"
    assert c.status == AppStatus.ready  # onaylı + e-posta -> hazır
    ev = [e for e in c.events if e.type == EventType.enrich]
    assert len(ev) == 1 and "2 e-posta" in ev[0].title


@respx.mock(assert_all_called=False)
def test_enrich_twice_is_idempotent(respx_mock, client, session):
    _mock_site(respx_mock)
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya", tech_stack=["Rust"])
    session.add(c)
    session.flush()
    website.enrich_company(c, session, client=client)
    n_contacts = len(c.contacts)
    stack = list(c.tech_stack)
    website.enrich_company(c, session, client=client)
    assert len(c.contacts) == n_contacts and c.tech_stack == stack and stack[0] == "Rust"
    assert len([e for e in c.events if e.type == EventType.enrich]) == 2


@respx.mock(assert_all_called=False)
def test_robots_disallow(respx_mock, client, session):
    _mock_site(respx_mock, robots="User-agent: *\nDisallow: /\n")
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya")
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=client)
    assert not res.ok and "robots" in res.error
    assert c.enriched_at is not None and c.contacts == []
    assert not any(str(call.request.url).endswith("/iletisim") for call in respx_mock.calls)


@respx.mock(assert_all_called=False)
def test_robots_partial_disallow(respx_mock, client):
    _mock_site(respx_mock, robots="User-agent: *\nDisallow: /kariyer\n")
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.pages_fetched == 3 and not res.has_internship_posting


@respx.mock(assert_all_called=False)
def test_unreachable_site(respx_mock, client, session):
    respx_mock.get("https://example.com/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
    respx_mock.get("http://example.com/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
    respx_mock.get("https://example.com/").mock(side_effect=httpx.ConnectTimeout("boom"))
    respx_mock.get("http://example.com/").mock(return_value=httpx.Response(503))
    c = Company(name="Ex", website="https://example.com/", city="Konya")
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=client)
    assert not res.ok and res.error == "Siteye ulaşılamadı"
    assert c.enriched_at is not None
    assert any(e.type == EventType.enrich and "Hata" in (e.body or "") for e in c.events)


@respx.mock(assert_all_called=False)
def test_http_fallback_when_https_fails(respx_mock, client):
    respx_mock.get("https://alfa.com.tr/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("http://alfa.com.tr/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://alfa.com.tr/").mock(side_effect=httpx.ConnectError("ssl"))
    respx_mock.get("http://alfa.com.tr/").mock(return_value=httpx.Response(200, text="<html><body>Python a@alfa.com.tr</body></html>"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.emails == ["a@alfa.com.tr"] and res.url.startswith("http://")


def test_no_website(session):
    c = Company(name="Yok", city="Konya")
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session)
    assert not res.ok and res.error == "Web sitesi yok" and c.enriched_at is not None


@respx.mock(assert_all_called=False)
def test_domain_only_company_uses_https(respx_mock, client):
    c = Company(name="D", domain="alfa.com.tr", city="Konya")
    assert website.start_url_for(c) == "https://alfa.com.tr"


@respx.mock(assert_all_called=False)
def test_non_html_and_huge_pages_skipped(respx_mock, client):
    respx_mock.get("https://alfa.com.tr/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("http://alfa.com.tr/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}))
    respx_mock.get("http://alfa.com.tr/").mock(return_value=httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}))
    res = website.crawl("https://alfa.com.tr/", client)
    assert not res.ok


@respx.mock(assert_all_called=False)
def test_crawl_never_raises_on_broken_html(respx_mock, client, session):
    respx_mock.get("https://alfa.com.tr/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(200, text="<<<>>><a href='http://[bad'>x</a><a href='mailto:'>y</a>", headers={"content-type": "text/html"}))
    c = Company(name="B", website="https://alfa.com.tr/", city="Konya")
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=client)
    assert res.ok and res.emails == []


def test_no_print():
    import inspect

    for mod in (website, scoring, keywords):
        assert "print(" not in inspect.getsource(mod)
