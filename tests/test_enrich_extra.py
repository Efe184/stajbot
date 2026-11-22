"""enrich/ için saldırgan (adversarial) testler: robots, yönlendirme, e-posta yanlış pozitifleri, kodlama,
anahtar kelime yanlış pozitifleri, çalışan sayısı deseni, dolu alan koruması, puan ağırlıkları.

Ağa asla çıkılmaz: tüm HTTP respx ile sahtelenir.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from stajbot import services
from stajbot.enrich import keywords, scoring, website
from stajbot.enums import AppStatus, ContactKind, EventType, ReviewStatus, SizeBucket
from stajbot.models import Company, CompanySource

HTML = {"content-type": "text/html; charset=utf-8"}


def _cf(email: str, key: int = 0x8E) -> str:
    return bytes([key] + [b ^ key for b in email.encode()]).hex()


def _page(body: str, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, text=f"<html><body>{body}</body></html>", headers=headers or HTML)


def _no_robots(m, *origins: str) -> None:
    for o in origins:
        m.get(f"{o}/robots.txt").mock(return_value=httpx.Response(404))


def _urls(m) -> list[str]:
    return [str(c.request.url) for c in m.calls]


@pytest.fixture()
def client():
    with httpx.Client(follow_redirects=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(website.Throttle, "wait", lambda self: None)


# --- robots.txt --------------------------------------------------------------------

@respx.mock(assert_all_called=False)
def test_robots_timeout_means_allowed(respx_mock, client):
    respx_mock.get("https://alfa.com.tr/robots.txt").mock(side_effect=httpx.ConnectTimeout("zaman aşımı"))
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page("info@alfa.com.tr"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.emails == ["info@alfa.com.tr"]


@respx.mock(assert_all_called=False)
def test_robots_disallow_only_other_bots(respx_mock, client):
    respx_mock.get("https://alfa.com.tr/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /gizli\n")
    )
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page('<a href="/gizli/iletisim">İletişim</a> <a href="/kariyer">Kariyer</a>'))
    respx_mock.get("https://alfa.com.tr/kariyer").mock(return_value=_page("ik@alfa.com.tr"))
    respx_mock.get("https://alfa.com.tr/gizli/iletisim").mock(return_value=_page("gizli@alfa.com.tr"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.emails == ["ik@alfa.com.tr"]
    assert not any(u.endswith("/gizli/iletisim") for u in _urls(respx_mock))


@respx.mock(assert_all_called=False)
def test_robots_fetched_once_per_origin(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page('<a href="/iletisim">İletişim</a><a href="/kariyer">Kariyer</a><a href="/hakkimizda">Hakkımızda</a>'))
    for p in ("iletisim", "kariyer", "hakkimizda"):
        respx_mock.get(f"https://alfa.com.tr/{p}").mock(return_value=_page("x"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.pages_fetched == 4
    assert sum(u.endswith("/robots.txt") for u in _urls(respx_mock)) == 1


@respx.mock(assert_all_called=False)
def test_robots_served_as_html_is_ignored(respx_mock, client):
    """Park edilmiş/yanlış yapılandırılmış siteler robots.txt yerine HTML sayfa döner: engel sayılmaz."""
    respx_mock.get("https://alfa.com.tr/robots.txt").mock(return_value=httpx.Response(200, text="<html>Disallow: /</html>", headers=HTML))
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page("info@alfa.com.tr"))
    assert website.crawl("https://alfa.com.tr/", client).ok


# --- yönlendirme / alan adı sınırı ---------------------------------------------------

@respx.mock(assert_all_called=False)
def test_redirect_to_other_domain_reads_landing_only(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr", "https://parked.example.net")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(301, headers={"location": "https://parked.example.net/"}))
    respx_mock.get("https://parked.example.net/").mock(
        return_value=_page('sales@parked.example.net <a href="/contact">Contact</a> <a href="/careers">Careers</a>')
    )
    respx_mock.get("https://parked.example.net/contact").mock(return_value=_page("x@parked.example.net"))
    respx_mock.get("https://parked.example.net/careers").mock(return_value=_page("intern"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.pages_fetched == 1 and res.landing_domain_differs
    urls = _urls(respx_mock)
    assert not any(u.endswith(("/contact", "/careers")) for u in urls)


@respx.mock(assert_all_called=False)
def test_redirected_landing_does_not_become_company_domain(respx_mock, client, session):
    _no_robots(respx_mock, "https://alfa.com.tr", "https://parked.example.net")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(302, headers={"location": "https://parked.example.net/"}))
    respx_mock.get("https://parked.example.net/").mock(return_value=_page("sales@parked.example.net"))
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya")
    session.add(c)
    session.flush()
    website.enrich_company(c, session, client=client)
    assert c.domain is None and c.website == "https://alfa.com.tr/"


@respx.mock(assert_all_called=False)
def test_subpage_redirecting_to_other_domain_is_ignored(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page('<a href="/kariyer">Kariyer</a>'))
    respx_mock.get("https://alfa.com.tr/kariyer").mock(return_value=httpx.Response(302, headers={"location": "https://www.kariyer.net/firma/alfa"}))
    respx_mock.get("https://www.kariyer.net/firma/alfa").mock(return_value=_page("destek@kariyer.net Stajyer alımı"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.pages_fetched == 1
    assert res.emails == [] and not res.has_internship_posting


@respx.mock(assert_all_called=False)
def test_redirect_to_www_is_same_site(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr", "https://www.alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(301, headers={"location": "https://www.alfa.com.tr/"}))
    respx_mock.get("https://www.alfa.com.tr/").mock(return_value=_page('<a href="/iletisim">İletişim</a>'))
    respx_mock.get("https://www.alfa.com.tr/iletisim").mock(return_value=_page("info@alfa.com.tr"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.pages_fetched == 2 and not res.landing_domain_differs and res.emails == ["info@alfa.com.tr"]


@respx.mock(assert_all_called=False)
def test_3xx_without_following_client_is_not_success(respx_mock):
    """Yönlendirme takip etmeyen istemcide 301 gövdesi 'başarılı boş sayfa' sayılmamalı."""
    _no_robots(respx_mock, "https://alfa.com.tr", "http://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(301, headers={"location": "https://www.alfa.com.tr/"}))
    respx_mock.get("http://alfa.com.tr/").mock(return_value=httpx.Response(301, headers={"location": "https://www.alfa.com.tr/"}))
    with httpx.Client(follow_redirects=False) as c:
        res = website.crawl("https://alfa.com.tr/", c)
    assert not res.ok and res.error == "Siteye ulaşılamadı"


def test_start_url_normalizes_schemeless_website():
    assert website.start_url_for(Company(name="x", website="alfa.com.tr")) == "https://alfa.com.tr"
    assert website.start_url_for(Company(name="x", website="www.alfa.com.tr/tr")) == "https://www.alfa.com.tr/tr"
    assert website.start_url_for(Company(name="x", website="mailto:a@b.com", domain="b.com")) == "https://b.com"
    assert website.start_url_for(Company(name="x")) is None


@respx.mock(assert_all_called=False)
def test_schemeless_website_is_crawled(respx_mock, client, session):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page("info@alfa.com.tr"))
    c = Company(name="Alfa", website="alfa.com.tr", city="Konya")
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=client)
    assert res.ok and res.emails == ["info@alfa.com.tr"]


def test_crawl_invalid_url_fails_cleanly(client):
    res = website.crawl("javascript:void(0)", client)
    assert not res.ok and res.error


# --- bağlantılar: göreli, tekilleştirme, sayfa sınırı ----------------------------------

@respx.mock(assert_all_called=False)
def test_relative_links_resolved(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/tr/index.html").mock(
        return_value=_page('<a href="iletisim.html">İletişim</a> <a href="../en/careers.html">Careers</a>')
    )
    respx_mock.get("https://alfa.com.tr/tr/iletisim.html").mock(return_value=_page("info@alfa.com.tr"))
    respx_mock.get("https://alfa.com.tr/en/careers.html").mock(return_value=_page("Internship open"))
    res = website.crawl("https://alfa.com.tr/tr/index.html", client)
    assert res.pages_fetched == 3 and res.emails == ["info@alfa.com.tr"]
    assert res.career_url == "https://alfa.com.tr/en/careers.html" and res.has_internship_posting


@respx.mock(assert_all_called=False)
def test_duplicate_links_fetched_once(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(
        return_value=_page('<a href="/kariyer">Kariyer</a><a href="/kariyer/">Kariyer</a><a href="/kariyer#top">Kariyer</a>'
                           '<a href="/KARIYER">Kariyer</a><a href="https://www.alfa.com.tr/kariyer">Kariyer</a><a href="/">Anasayfa</a>')
    )
    route = respx_mock.get(url__regex=r"https://(www\.)?alfa\.com\.tr/kariyer/?").mock(return_value=_page("Stajyer"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.pages_fetched == 2 and route.call_count == 1 and res.has_internship_posting


@respx.mock(assert_all_called=False)
def test_max_pages_bound(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr")
    links = "".join(f'<a href="/kariyer-{i}">Kariyer {i}</a>' for i in range(12))
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page(links))
    sub = respx_mock.get(url__regex=r"https://alfa\.com\.tr/kariyer-\d+").mock(return_value=_page("x"))
    res = website.crawl("https://alfa.com.tr/", client, max_pages=3)
    assert res.pages_fetched == 3 and sub.call_count == 2
    res1 = website.crawl("https://alfa.com.tr/", client, max_pages=1)
    assert res1.pages_fetched == 1


@respx.mock(assert_all_called=False)
def test_turkish_link_text_matches_subpages(respx_mock, client):
    """'İletişim', 'İnsan Kaynakları' gibi Türkçe bağlantı metinleri (yol ipucu olmadan) tanınmalı."""
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(
        return_value=_page('<a href="/sayfa/12">İletişim</a> <a href="/sayfa/7">İnsan Kaynakları</a> <a href="/sayfa/9">Ürünler</a>')
    )
    respx_mock.get("https://alfa.com.tr/sayfa/12").mock(return_value=_page("info@alfa.com.tr"))
    respx_mock.get("https://alfa.com.tr/sayfa/7").mock(return_value=_page("Staj başvuruları: ik@alfa.com.tr"))
    respx_mock.get("https://alfa.com.tr/sayfa/9").mock(return_value=_page("urun@alfa.com.tr"))
    res = website.crawl("https://alfa.com.tr/", client)
    assert set(res.emails) == {"info@alfa.com.tr", "ik@alfa.com.tr"}
    assert res.career_url == "https://alfa.com.tr/sayfa/7" and res.has_internship_posting
    assert not any(u.endswith("/sayfa/9") for u in _urls(respx_mock))


@respx.mock(assert_all_called=False)
def test_external_career_page_not_fetched_but_recorded(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page('<a href="https://www.kariyer.net/firma/alfa">Açık Pozisyonlar</a>'))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.career_url == "https://www.kariyer.net/firma/alfa"
    assert not any("kariyer.net" in u for u in _urls(respx_mock))


# --- e-posta çıkarma -----------------------------------------------------------------

def test_email_false_positives_rejected():
    html = """
    <img src="/img/logo@2x.png"> <img src="/img/icon@3x.avif">
    <script>Sentry.init({dsn:"https://3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c@o123456.ingest.sentry.io/4505"})</script>
    <script src="https://unpkg.com/react@18.2.0/umd/react.production.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/htmx.org@1.9.10/dist/htmx.min.js"></script>
    a1b2c3@sentry-next.wixpress.com  user@example.com  ornek@ornek.com  adiniz@alfa.com.tr  ad.soyad@alfa.com.tr
    webmaster@alfa.com.tr  postmaster@alfa.com.tr  gercek.kisi@alfa.com.tr  Info@Alfa.com.tr.
    """
    emails = website.extract_emails(html)
    assert emails == ["gercek.kisi@alfa.com.tr", "info@alfa.com.tr"]


def test_mailto_urlencoded_and_params():
    html = '<a href="mailto:%20Info@Alfa.com.tr?subject=Staj%20Ba%C5%9Fvurusu&cc=ik@alfa.com.tr">yaz</a>'
    assert website.extract_emails(html) == ["info@alfa.com.tr", "ik@alfa.com.tr"]
    assert website.extract_emails('<a href="mailto:%20">boş</a> <a href="mailto:info%40alfa.com.tr">x</a>') == ["info@alfa.com.tr"]


def test_cfemail_href_variant_and_invalid():
    html = f'<a href="/cdn-cgi/l/email-protection#{_cf("ik@alfa.com.tr")}">[email protected]</a>'
    assert website.extract_emails(html) == ["ik@alfa.com.tr"]
    assert website.extract_emails('<a href="/cdn-cgi/l/email-protection#zz">x</a>') == []
    assert website.decode_cfemail("") is None


def test_obfuscated_variants():
    text = "kariyer (at) alfa (dot) com (dot) tr ; ik [AT] alfa [DOT] com ; info{at}alfa{dot}com.tr ; destek(et)alfa.com.tr"
    assert set(website.extract_emails("", text)) == {"kariyer@alfa.com.tr", "ik@alfa.com", "info@alfa.com.tr", "destek@alfa.com.tr"}


def test_html_entity_at_sign_via_text():
    tree = website.HTMLParser("<html><body>Bize yazın: info&#64;alfa.com.tr</body></html>")
    text = website.visible_text(tree)
    assert website.extract_emails("", text) == ["info@alfa.com.tr"]


def test_prioritize_emails_company_domain_first_and_foreign_dropped():
    found = ["ik@teknopark.com.tr", "destek@iyzico.com", "kisi@gmail.com", "info@alfa.com.tr", "ik@mail.alfa.com.tr"]
    assert website.prioritize_emails(found, ["alfa.com.tr"]) == ["info@alfa.com.tr", "ik@mail.alfa.com.tr", "kisi@gmail.com"]
    # kendi alan adında e-posta yoksa yabancılar son sırada korunur
    assert website.prioritize_emails(["ik@teknopark.com.tr", "kisi@gmail.com"], ["alfa.com.tr"]) == ["kisi@gmail.com", "ik@teknopark.com.tr"]
    assert len(website.prioritize_emails([f"a{i}@alfa.com.tr" for i in range(30)], ["alfa.com.tr"])) == website.MAX_EMAILS


@respx.mock(assert_all_called=False)
def test_foreign_domain_email_never_becomes_primary(respx_mock, client, session):
    """Sayfadaki teknopark İK adresi şirketin birincil kişisi olmamalı."""
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page("ik@teknopark.com.tr destek@iyzico.com info@alfa.com.tr"))
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya", review_status=ReviewStatus.approved)
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=client)
    assert res.emails == ["info@alfa.com.tr"] and res.new_contacts == 1
    assert c.primary_contact.email == "info@alfa.com.tr" and c.primary_contact.kind == ContactKind.info


# --- içerik türü / boyut / kodlama -------------------------------------------------------

@respx.mock(assert_all_called=False)
def test_pdf_subpage_skipped_even_with_html_content_type(respx_mock, client):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page('<a href="/kariyer">Kariyer</a><a href="/iletisim">İletişim</a>'))
    respx_mock.get("https://alfa.com.tr/kariyer").mock(return_value=httpx.Response(200, content=b"%PDF-1.4 stajyer", headers=HTML))
    respx_mock.get("https://alfa.com.tr/iletisim").mock(return_value=httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.pages_fetched == 1 and not res.has_internship_posting


@respx.mock(assert_all_called=False)
def test_huge_page_stops_reading_and_is_skipped(respx_mock, client, monkeypatch):
    monkeypatch.setattr(website, "MAX_BYTES", 2_000)
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page('<a href="/iletisim">İletişim</a><a href="/kariyer">Kariyer</a>'))
    big = "<p>info@alfa.com.tr</p>" + "x" * 5_000
    respx_mock.get("https://alfa.com.tr/iletisim").mock(return_value=_page(big))
    respx_mock.get("https://alfa.com.tr/kariyer").mock(return_value=httpx.Response(200, text="<html>küçük</html>", headers={**HTML, "content-length": "99999999"}))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and res.pages_fetched == 1 and res.emails == []


@respx.mock(assert_all_called=False)
def test_cp1254_meta_charset_without_header(respx_mock, client):
    body = '<html><head><meta charset="windows-1254"></head><body>Yazılım geliştiriyoruz, gömülü sistemler. 50 çalışan. ik@alfa.com.tr</body></html>'.encode("cp1254")
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(200, content=body, headers={"content-type": "text/html"}))
    res = website.crawl("https://alfa.com.tr/", client)
    assert "Gömülü sistemler" in res.tech_stack and res.employee_count == 50 and res.emails == ["ik@alfa.com.tr"]


@respx.mock(assert_all_called=False)
def test_invalid_utf8_without_charset_falls_back(respx_mock, client):
    body = "<html><body>Gömülü yazılım, 120 mühendis</body></html>".encode("iso-8859-9")
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=httpx.Response(200, content=body, headers={"content-type": "text/html"}))
    res = website.crawl("https://alfa.com.tr/", client)
    assert res.ok and "Gömülü sistemler" in res.tech_stack and res.employee_count == 120


def test_decode_body_header_wins_and_unknown_charset_is_safe():
    s = "çalışan"
    assert website._decode_body(s.encode("cp1254"), "text/html; charset=ISO-8859-9") == s
    assert website._decode_body(s.encode("utf-8"), "text/html; charset=x-bilinmeyen") == s
    assert "�" not in website._decode_body(b"\xff\xfe abc", "text/html")


# --- teknoloji ipuçları ------------------------------------------------------------------

@respx.mock(assert_all_called=False)
def test_script_src_hints_survive_visible_text(respx_mock, client):
    """Sayfa metninde geçmeyen ama script kaynağında görülen teknoloji (Angular) yakalanmalı."""
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(
        return_value=httpx.Response(200, text='<html><head><script src="/js/angular.min.js"></script></head><body>Merhaba</body></html>', headers=HTML)
    )
    res = website.crawl("https://alfa.com.tr/", client)
    assert "Angular" in res.tech_stack


def test_keyword_false_positives():
    t = keywords.detect_technologies
    assert "Telekom" not in t("GSM: 0532 123 45 67 Tel: 0212 444")
    assert "Telekom" in t("GSM şebeke altyapısı ve 5G")
    assert "Docker" not in t("Konteyner taşımacılığı ve container lojistiği")
    assert "Docker" in t("Docker ile containerization")
    assert "iOS" not in t("IBAN: TR00 SWIFT: TGBATRIS")
    assert "iOS" in t("iOS ve SwiftUI")
    assert "Rust" not in t("trust, rust-free, Rustproof")
    assert "Rust" in t("Rust dili")
    assert "Go" not in t("Let's go! go ahead")
    assert "C" not in t("C vitamini ve C sınıfı")
    assert "C" in t("C/C++ ile gömülü")
    assert "Java" not in t("javascript javascript")
    assert ".NET" not in t("www.alfa.net ve info@alfa.net")
    assert "Yapay zeka" not in t("Air ve airline")
    assert "Blockchain" not in t("kriptografi ve cryptography")


def test_keyword_turkish_folding():
    techs = keywords.detect_technologies("SİBER GÜVENLİK, GÖMÜLÜ SİSTEMLER, YAPAY ZEKÂ, Veri Bilimi, E-Ticaret")
    assert {"Siber güvenlik", "Gömülü sistemler", "Yapay zeka", "Veri bilimi", "E-ticaret"} <= set(techs)


def test_detect_sector_ties_and_generic():
    assert keywords.detect_sector("Bilişim danışmanlığı hizmetleri") == "Bilişim danışmanlığı"
    assert keywords.detect_sector("savunma savunma fintech") == "Savunma"
    assert keywords.detect_sector("ürünlerimiz hakkında bilgi") is None


# --- çalışan sayısı -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("10.000+ çalışan", 10000),
        ("1.500 mühendis", 1500),
        ("100'ü aşkın personel", 100),
        ("2.500'ün üzerinde çalışan", 2500),
        ("1000'den fazla çalışanımız", 1000),
        ("Tel: 0212 555 12 34 Personel Girişi", None),
        ("Faks: 0312 222 33 44 Personel", None),
        ("%100 yerli üretim", None),
        ("2010 yılında kurulduk", None),
        ("3 kişilik ekip", None),
        ("Konya Teknokent No: 12 Uzman", None),
        ("500+ müşteri, 60 çalışan", 60),
        ("150 kişiden oluşan ekibimiz", 150),
    ],
)
def test_employee_count_patterns(text, expected):
    assert website.find_employee_count(text) == expected


# --- veritabanı yazımı: dolu alan koruması, enriched_at, Event ---------------------------------

@respx.mock(assert_all_called=False)
def test_filled_fields_never_overwritten(respx_mock, client, session):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(
        return_value=_page('<a href="/kariyer">Kariyer</a><a href="https://www.linkedin.com/company/alfa-yeni/">in</a> Savunma sanayi. 500+ çalışan')
    )
    respx_mock.get("https://alfa.com.tr/kariyer").mock(return_value=_page("Stajyer arıyoruz"))
    c = Company(
        name="Alfa", website="https://alfa.com.tr/", domain="alfa.com.tr", city="Konya", sector="Fintech",
        career_url="https://alfa.com.tr/eski-kariyer", linkedin_url="https://www.linkedin.com/company/alfa-eski",
        employee_count=42, size_bucket=SizeBucket.s11_50, internship_posting_url="https://alfa.com.tr/ilan",
    )
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=client)
    assert res.ok and res.employee_count == 500
    assert c.sector == "Fintech" and c.career_url == "https://alfa.com.tr/eski-kariyer"
    assert c.linkedin_url == "https://www.linkedin.com/company/alfa-eski"
    assert c.employee_count == 42 and c.size_bucket == SizeBucket.s11_50
    assert c.internship_posting_url == "https://alfa.com.tr/ilan" and c.has_internship_posting
    assert c.website == "https://alfa.com.tr/" and c.domain == "alfa.com.tr"


def test_size_bucket_not_overwritten_when_only_count_missing(session):
    c = Company(name="Z", city="Konya", size_bucket=SizeBucket.s1000_plus)
    session.add(c)
    session.flush()
    website.apply_result(c, website.EnrichResult(url="https://z.com", employee_count=20), session)
    assert c.employee_count == 20 and c.size_bucket == SizeBucket.s1000_plus
    c2 = Company(name="Z2", city="Konya")
    session.add(c2)
    session.flush()
    website.apply_result(c2, website.EnrichResult(url="https://z2.com", employee_count=20), session)
    assert c2.size_bucket == SizeBucket.s11_50


def test_apply_failure_still_sets_enriched_at_and_event(session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("patladı")

    monkeypatch.setattr(website, "crawl", lambda *a, **k: website.EnrichResult(url="https://alfa.com.tr/", emails=["ik@alfa.com.tr"]))
    monkeypatch.setattr(website, "apply_result", boom)
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya", review_status=ReviewStatus.approved)
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=httpx.Client())
    assert not res.ok and res.error.startswith("Sonuç yazılamadı")
    assert c.enriched_at is not None
    ev = [e for e in c.events if e.type == EventType.enrich]
    assert len(ev) == 1 and "Hata" in (ev[0].body or "") and "başarısız" in ev[0].title
    assert c.status == AppStatus.found  # kişi eklenemedi, hazır olamaz


def test_crawl_exception_is_swallowed(session, monkeypatch):
    def boom(*a, **k):
        raise ValueError("kötü sayfa")

    monkeypatch.setattr(website, "crawl", boom)
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya")
    session.add(c)
    session.flush()
    res = website.enrich_company(c, session, client=httpx.Client())
    assert not res.ok and "ValueError" in res.error and c.enriched_at is not None
    assert any(e.type == EventType.enrich for e in c.events)


@respx.mock(assert_all_called=False)
def test_not_approved_company_stays_found(respx_mock, client, session):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page("ik@alfa.com.tr"))
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya")
    session.add(c)
    session.flush()
    website.enrich_company(c, session, client=client)
    assert c.primary_contact is not None and c.status == AppStatus.found
    services.approve_company(session, c)
    assert c.status == AppStatus.ready


@respx.mock(assert_all_called=False)
def test_domain_not_stolen_from_other_company(respx_mock, client, session):
    _no_robots(respx_mock, "https://alfa.com.tr")
    respx_mock.get("https://alfa.com.tr/").mock(return_value=_page("x"))
    other = Company(name="Diğer", domain="alfa.com.tr", city="Konya")
    c = Company(name="Alfa", website="https://alfa.com.tr/", city="Konya")
    session.add_all([other, c])
    session.flush()
    website.enrich_company(c, session, client=client)
    assert c.domain is None and other.domain == "alfa.com.tr"


def test_summary_turkish():
    ok = website.EnrichResult(pages_fetched=2, emails=["a@b.com"], tech_stack=["Python"], career_url="x", has_internship_posting=True, employee_count=50, landing_domain_differs=True)
    s = ok.summary()
    assert s.startswith("Zenginleştirildi") and "staj ilanı" in s and "çalışan" in s and "yönlendirdi" in s
    assert website.EnrichResult(ok=False, error="x").summary().startswith("Zenginleştirme başarısız")


# --- puanlama: ağırlıklar tek tek, sıkıştırma, açıklama ---------------------------------------

def _co(session, **kw) -> Company:
    base = dict(name="X", city="Konya")
    base.update(kw)
    c = Company(**base)
    session.add(c)
    session.flush()
    return c


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({}, 0),
        ({"has_internship_posting": True}, 25),
        ({"is_arge_center": True}, 15),
        ({"is_teknopark": True}, 10),
        ({"school_protocol": True}, 25),
        ({"size_bucket": SizeBucket.s11_50}, 5),
        ({"size_bucket": SizeBucket.s51_200}, 5),
        ({"size_bucket": SizeBucket.s201_1000}, 5),
        ({"size_bucket": SizeBucket.s1_10}, 0),
        ({"size_bucket": SizeBucket.s1000_plus}, 0),
        ({"size_bucket": SizeBucket.unknown}, 0),
    ],
)
def test_score_single_weights(session, kw, expected):
    assert scoring.compute_score(_co(session, **kw), []) == expected


def test_score_sources_and_tech_cap(session):
    c = _co(session)
    session.add(CompanySource(company=c, source="argem"))
    session.flush()
    assert scoring.compute_score(c, []) == 0  # tek kaynak: puan yok
    session.add(CompanySource(company=c, source="teknopark"))
    session.flush()
    assert scoring.compute_score(c, []) == 10
    c.tech_stack = ["Python", "Java", "Go", "Rust", "AWS", "Azure"]
    assert scoring.compute_score(c, ["Python"]) == 15
    assert scoring.compute_score(c, ["python", "JAVA", "go", "rust", "aws", "azure"]) == 30  # tavan 20
    assert scoring.compute_score(c, ["Kotlin"]) == 10


def test_score_email_kinds(session):
    c = _co(session)
    services.add_contact(session, c, "ahmet.yilmaz@x.com", source="t")  # personal -> "yalnızca genel" +5
    assert scoring.compute_score(c, []) == 5
    services.add_contact(session, c, "staj@x.com", source="t")
    assert scoring.compute_score(c, []) == 15


def test_score_city_variants_and_clamp(session):
    assert scoring.compute_score(_co(session, city="ISTANBUL", is_arge_center=True), []) == 15
    assert scoring.compute_score(_co(session, city="Afyon", is_arge_center=True), []) == 15
    assert scoring.compute_score(_co(session, city=None, is_arge_center=True), []) == 0
    c = _co(session, is_arge_center=True, is_teknopark=True, school_protocol=True, has_internship_posting=True, size_bucket=SizeBucket.s51_200, tech_stack=["Python", "Java", "Go", "Rust"])
    session.add_all([CompanySource(company=c, source="a"), CompanySource(company=c, source="b")])
    services.add_contact(session, c, "ik@x.com", source="t")
    assert 0 <= scoring.compute_score(c, ["Python", "Java", "Go", "Rust"]) == 100


def test_explain_is_turkish_and_consistent(session):
    c = _co(session, is_arge_center=True, has_internship_posting=True, tech_stack=["Python"])
    lines = scoring.explain(c, ["Python"])
    assert lines[-1] == f"Toplam: {scoring.compute_score(c, ['Python'])}"
    assert any("Ar-Ge" in ln and "+15" in ln for ln in lines)
    assert any("Staj ilanı" in ln and "+25" in ln for ln in lines)
    assert any("Python" in ln and "+5" in ln for ln in lines)
    assert all(ln.strip() for ln in lines)
    out = scoring.explain(_co(session, city="Trabzon"), [])
    assert out[0].startswith("Hedef şehir dışı") and out[-1] == "Toplam: 0"


def test_explain_without_profile_file_does_not_crash(session, monkeypatch, tmp_path):
    monkeypatch.setattr(scoring, "profile_techs", lambda path=None: [])
    c = _co(session, tech_stack=["Python"])
    assert scoring.compute_score(c, None) == 0
    assert scoring.explain(c, None)[-1] == "Toplam: 0"
