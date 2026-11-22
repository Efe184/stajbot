"""bilisim500 toplayıcıları (YASAD / TÜBİSAD / Bilişim 500 CSV) için fikstür tabanlı testler."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from stajbot.collectors import bilisim500 as b
from stajbot.collectors.base import RawCompany, Throttle, make_http_client

FIX = Path(__file__).parent / "fixtures" / "bilisim500"


# --- yardımcı fikstürler -----------------------------------------------------------

@pytest.fixture(scope="module")
def rest_records() -> list[dict]:
    return json.loads((FIX / "yasad_firmalar_rest_sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tax() -> dict[str, list[dict]]:
    return {
        name: json.loads((FIX / f"yasad_tax_{name}.json").read_text(encoding="utf-8"))
        for name in ("lokasyon", "sektor-kategorisi", "firma-durum")
    }


@pytest.fixture(scope="module")
def tubisad_html() -> str:
    return (FIX / "tubisad_uye_listesi.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def listing_html() -> str:
    return (FIX / "yasad_uyelerimiz_page1_trimmed.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (FIX / "yasad_firma_detail_architecht.html").read_text(encoding="utf-8")


@pytest.fixture()
def parsed(rest_records, tax) -> dict[int, RawCompany]:
    out = b.parse_yasad_rest(rest_records, lokasyon=tax["lokasyon"], sektor=tax["sektor-kategorisi"])
    return {r.raw["id"]: r for r in out}


def _robots_ok(router: respx.MockRouter) -> None:
    router.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))


def _rest_handler(records: list[dict], total_pages: int = 1):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page > total_pages:
            return httpx.Response(400, json={"code": "rest_post_invalid_page_number"})
        return httpx.Response(
            200, json=records, headers={"X-WP-Total": str(len(records) * total_pages), "X-WP-TotalPages": str(total_pages)}
        )

    return handler


def _fast_client() -> httpx.Client:
    return make_http_client()


# --- yardımcı fonksiyonlar -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://https://www.bink.com.tr", "https://www.bink.com.tr"),
        ("www.logo.com.tr", "https://www.logo.com.tr"),
        ("zirveyazilim.net", "https://zirveyazilim.net"),
        ("https://www.basarsoft.com.tr/tr/", "https://www.basarsoft.com.tr/tr/"),
        ("  http://www.olbia.com.tr  ", "http://www.olbia.com.tr"),
        ("http://", None),
        ("https://", None),
        ("www", None),
        ("", None),
        (None, None),
        (12345, None),
        ("bir web sitesi yok", None),
    ],
)
def test_normalize_website(raw, expected):
    assert b.normalize_website(raw) == expected


def test_site_origin_reduces_deep_links():
    assert b.site_origin("https://www.atlantisbilgisayar.net/sayfa/hakkinda") == "https://www.atlantisbilgisayar.net"
    assert b.site_origin("http://www.adesso.com.tr/") == "http://www.adesso.com.tr"
    assert b.site_origin("apple.com/tr") == "https://apple.com"
    assert b.site_origin("http://") is None


@pytest.mark.parametrize(
    "text, city, district, method",
    [
        ("Çifte Havuzlar Mah. Eski Londra Asfaltı Cad. Kuluçka Merkezi Esenler İstanbul", "İstanbul", "Esenler", "province"),
        ("Palmiye Mh. Adnan Menderes Blv. No:34 Yenişehir Mersin", "Mersin", "Yenişehir", "province"),
        ("Büyükkayacık Osb Mah. 101. Cad. No: 4 Selçuklu/ Konya", "Konya", "Selçuklu", "province"),
        ("ETGB Teknoparkı Kat:2 Odunpazarı / Eskişehir / Türkiye", "Eskişehir", "Odunpazarı", "province"),
        ("Serdivan / SAKARYA", "Sakarya", "Serdivan", "province"),
        ("Afyon Merkez", "Afyonkarahisar", None, "province"),
        # Son il eşleşmesi alınır
        ("Bilkent Cyberpark Çankaya/Ankara iSTANBUL OFİS: Kadıköy/İstanbul", "İstanbul", "Kadıköy", "province"),
        # İl adlı sokak/yol il sayılmaz; ilçeden çözülür
        ("Ankara Caddesi No:5 Kartal", "İstanbul", "Kartal", "district"),
        ("Bursa Yolu 5. km Nilüfer", "Bursa", "Nilüfer", "district"),
        # Yalnızca ilçe / teknokent
        ("Sarıyer", "İstanbul", "Sarıyer", "district"),
        ("ODTÜ Teknokent Galyum Blok", "Ankara", "Çankaya", "district"),
        ("Bilişim Vadisi Muallimköy", "Kocaeli", "Gebze", "district"),
        ("Kocasinan", "Kayseri", "Kocasinan", "district"),
        # Çözülemez
        ("Sanayi Mah. Teknopark Blv. 1 / 4C / 601 Teknopark", None, None, None),
        ("", None, None, None),
        (None, None, None, None),
    ],
)
def test_city_from_text(text, city, district, method):
    g = b.city_from_text(text)
    assert (g.city, g.district, g.method) == (city, district, method)


def test_resolve_city_prefers_lokasyon_term(tax):
    lok = b.term_map(tax["lokasyon"])
    g = b.resolve_city([52], "Nidakule Göztepe Kadıköy/İstanbul", lok)
    assert g.city == "Ankara" and g.method == "lokasyon"
    # GEBZE terimi Kocaeli'ye normalize edilir
    assert b.resolve_city([210], None, lok).city == "Kocaeli"
    # terim yoksa adres
    g2 = b.resolve_city([], "Konak / İzmir", lok)
    assert g2.city == "İzmir" and g2.district == "Konak" and g2.method == "province"
    # hiçbiri yoksa None
    assert b.resolve_city(None, None, lok).city is None


def test_teknopark_hint():
    assert b.teknopark_hint("Akdeniz Üni. Antalya Teknokent Ar-Ge2 Konyaaltı") == (True, "Antalya Teknokent")
    assert b.teknopark_hint("Bilişim Vadisi Muallimköy Mah. Kocaeli") == (True, "Bilişim Vadisi")
    assert b.teknopark_hint("Sakarya Üniversitesi Teknoloji Geliştirme Bölgesi No:10D") == (True, "Sakarya Teknokent")
    assert b.teknopark_hint("Gebze Organize Sanayi Bölgesi No 609") == (False, None)
    assert b.teknopark_hint(None) == (False, None)


def test_term_map_handles_lists_dicts_and_entities(tax):
    m = b.term_map(tax["sektor-kategorisi"])
    assert m[132] == "ERP & PLM"
    assert b.term_map({"80": "İstanbul"}) == {80: "İstanbul"}
    assert b.term_map(None) == {}
    assert b.term_map([{"id": "x", "name": "bozuk"}, {"name": "idsiz"}, "çöp"]) == {}


# --- YASAD REST ---------------------------------------------------------------------

def test_parse_yasad_rest_counts_and_types(rest_records, tax, parsed):
    assert len(parsed) == len(rest_records) == 15
    for r in parsed.values():
        assert isinstance(r, RawCompany)
        assert r.source == "yasad"
        assert r.name and r.source_url and r.source_url.startswith("https://www.yasad.org.tr/firmalar/")
        assert "yasad" in r.tags
        assert r.raw["via"] == "rest"
        assert r.raw["city_unknown"] == (r.city is None)
        for e in r.emails:
            assert "@" in e and e == e.lower()


def test_parse_yasad_rest_logo(parsed):
    r = parsed[6878]
    assert r.name == "Logo Yazılım"
    assert r.raw["legal_name"] == "Logo Yazılım San. Ve Tic.A.Ş."
    assert r.city == "Kocaeli" and r.district == "Gebze" and r.raw["city_method"] == "lokasyon"
    assert r.website == "https://www.logo.com.tr"
    assert r.sector == "ERP & PLM"
    assert r.emails == ["iletisim@logo.com.tr"]
    assert r.raw["member_class"] == "A" and r.raw["is_member"] is True and r.raw["is_catalog"] is True
    assert {"yasad-uye", "yasad-katalog", "yasad-sinif-a", "ERP & PLM"} <= set(r.tags)
    assert r.raw["founded_year"] == 1984
    assert "sehir-bilinmiyor" not in r.tags


def test_parse_yasad_rest_city_from_address_when_lokasyon_empty(parsed):
    assert parsed[6778].city == "Konya" and parsed[6778].raw["city_method"] == "province"
    assert parsed[6854].city == "Mersin" and parsed[6854].district == "Yenişehir"
    assert parsed[9938].city == "İstanbul" and parsed[9938].district == "Esenler"
    assert parsed[9935].city == "İstanbul" and parsed[9935].district == "Ataşehir"
    assert parsed[6697].city == "Kayseri" and parsed[6697].raw["city_method"] == "district"


def test_parse_yasad_rest_target_cities_present(parsed):
    cities = {parsed[i].city for i in (6778, 7081, 6956, 6919, 6708, 6934, 6854, 9938, 9613)}
    assert cities == {"Konya", "Ankara", "Eskişehir", "Sakarya", "Antalya", "Mersin", "İstanbul", "İzmir"}


def test_parse_yasad_rest_field_cleanup(parsed):
    # "http://https://" yazım hatası düzeltilir
    assert parsed[6697].website == "https://www.bink.com.tr"
    # şema eksikse https eklenir
    assert parsed[7081].website == "https://zirveyazilim.net"
    # int telefon metne çevrilir ve başına 0 eklenir
    assert parsed[9938].phone == "02129630181"
    assert parsed[6854].phone == "0324 327 31 54"
    # Sirket_Unvani boş -> title; title boş -> Sirket_Unvani
    assert parsed[6854].name.startswith("Kardelen")
    assert parsed[9919].name.startswith("Vispera")
    # linkedin yalnızca linkedin.com bağlantısıysa
    assert parsed[6681].linkedin_url == "https://www.linkedin.com/company/basarsoft/"
    assert parsed[6878].linkedin_url is None
    # yetkili e-postası yalnızca kurumsal (info@) ise alınır, kişisel alınmaz
    assert parsed[7058].emails == ["info@veripark.com"]
    # kategori HTML entity çözülür
    assert parsed[6934].sector == "ERP & PLM"
    # teknokent ipucu
    assert parsed[6934].is_teknopark and parsed[6934].teknopark_name == "Antalya Teknokent"
    assert parsed[6919].is_teknopark and parsed[6919].teknopark_name == "Bilkent Cyberpark"
    assert parsed[6878].is_teknopark is False
    # açıklama kısaltılır
    assert parsed[9613].description and len(parsed[9613].description) <= 1000


def test_parse_yasad_rest_without_taxonomies_uses_fallback(rest_records):
    out = b.parse_yasad_rest(rest_records)
    by_id = {r.raw["id"]: r for r in out}
    assert by_id[6708].city == "Sakarya"
    assert by_id[6878].sector == "ERP & PLM"


def test_parse_yasad_rest_skips_nameless_and_marks_unknown_city():
    recs = [
        {"id": 1, "title": {"rendered": ""}, "acf": {"Sirket_Unvani": ""}},
        {"id": 2, "title": {"rendered": "Test Yazılım"}, "acf": {"Sirket_Adres": "Teknopark Binası", "telefon": None, "web_sitesi_adresi": "http://"}},
        "çöp",
        {"id": 3, "title": {"rendered": "ACF'siz Firma"}},
    ]
    out = b.parse_yasad_rest(recs)
    assert [r.raw["id"] for r in out] == [2, 3]
    r = out[0]
    assert r.city is None and "sehir-bilinmiyor" in r.tags and r.raw["city_unknown"] is True
    assert r.website is None and r.phone is None and r.is_teknopark is True


# --- YASAD HTML yedek ----------------------------------------------------------------

def test_parse_yasad_listing(listing_html):
    cards, next_url = b.parse_yasad_listing(listing_html)
    assert len(cards) == 6
    assert next_url == "https://www.yasad.org.tr/uyelerimiz/page/2/"
    assert cards[0].name == "2 Adam Yazılım ve Teknoloji"
    assert cards[0].detail_url == "https://www.yasad.org.tr/firmalar/2-adam-yazilim/"
    assert cards[0].logo_alt == "2adam yazılım"
    assert cards[1].name == "3S YAZILIM VE BİLİŞİM HİZMETLERİ"
    assert all(c.detail_url and "/firmalar/" in c.detail_url for c in cards)


def test_parse_yasad_detail(detail_html):
    d = b.parse_yasad_detail(detail_html, "https://www.yasad.org.tr/firmalar/architecht/")
    assert d["name"] == "Architecht"
    assert d["website"] == "http://www.architecht.com"
    assert d["phone"] == "0 (216)-585-99-70"
    assert d["address"] == "Sanayi Mah. Teknopark Blv. 1 / 4C / 601 Teknopark"
    assert d["member_class"] == "B"
    assert d["founded_year"] is None


def test_yasad_from_html(listing_html, detail_html):
    cards, _ = b.parse_yasad_listing(listing_html)
    detail = b.parse_yasad_detail(detail_html)
    r = b.yasad_from_html(cards[0], detail)
    assert r.source == "yasad" and r.name == "2 Adam Yazılım ve Teknoloji"
    assert r.website == "http://www.architecht.com"
    assert r.city is None and "sehir-bilinmiyor" in r.tags
    assert "yasad-uye" in r.tags and "yasad-sinif-b" in r.tags
    assert r.raw["via"] == "html" and r.raw["member_class"] == "B"


# --- TÜBİSAD ---------------------------------------------------------------------------

def test_parse_tubisad(tubisad_html):
    out = b.parse_tubisad(tubisad_html)
    assert len(out) >= 235
    by_site = {r.website: r for r in out if r.website}
    adesso = by_site["http://www.adesso.com.tr"]
    assert adesso.name == "Adesso" and adesso.raw["slug"] == "adesso" and adesso.raw["name_source"] == "logo-slug"
    assert "ad-tahmini" in adesso.tags
    afn = by_site["https://afnteknoloji.com"]
    assert afn.name.startswith("AFN TEKNOLOJİ") and afn.raw["name_source"] == "span" and "ad-tahmini" not in afn.tags
    assert "https://www.ardbilisim.com.tr" in by_site and "https://www.architecht.com" in by_site
    # derin bağlantı kök adrese indirgenir
    assert "https://www.atlantisbilgisayar.net" in by_site
    assert by_site["https://www.atlantisbilgisayar.net"].raw["original_href"].endswith("/sayfa/hakkinda")
    # LinkedIn bağlantısı web sitesi değil
    li = [r for r in out if r.linkedin_url]
    assert li and li[0].website is None and "linkedin.com" in li[0].linkedin_url
    for r in out:
        assert r.source == "tubisad" and r.city is None
        assert {"tubisad", "tubisad-uye", "sehir-bilinmiyor"} <= set(r.tags)
        assert r.raw["city_unknown"] is True
    # aynı alan adı iki kez üretilmez
    sites = [r.website for r in out if r.website]
    assert len(sites) == len(set(sites))


def test_parse_tubisad_tolerates_odd_cells():
    html = """<div class="HTMLContent"><table><tr>
    <td><a href="https://ornek.com.tr/"><img src="/tr/images/uyeler/logo-ornek.png?1"></a></td>
    <td><a href="mailto:x@y.com">mail</a></td>
    <td><a href="https://sadece-metin.com">Sadece Metin A.Ş.</a></td>
    <td><a href="https://altli.com"><img alt="Altlı Yazılım" src="/x/y.png"></a></td>
    <td><a href="https://isimsiz.com"><img src="/x/z.png"></a></td>
    <td></td></tr></table></div>"""
    out = b.parse_tubisad(html, "http://test")
    names = {r.website: (r.name, r.raw["name_source"]) for r in out}
    assert names["https://ornek.com.tr"] == ("Ornek", "logo-slug")
    assert names["https://sadece-metin.com"] == ("Sadece Metin A.Ş.", "text")
    assert names["https://altli.com"] == ("Altlı Yazılım", "alt")
    assert names["https://isimsiz.com"] == ("Isimsiz", "domain")
    assert len(out) == 4
    assert b.parse_tubisad("<html><body>boş</body></html>") == []


# --- Bilişim 500 CSV -------------------------------------------------------------------

def test_parse_bilisim500_csv_and_collector(tmp_path: Path):
    manual = tmp_path / "manual"
    manual.mkdir()
    p = manual / "bilisim500_2025.csv"
    p.write_text(
        "Sıra;Şirket;Kategori;Ciro;Şehir;Web\n"
        "1;Türk Telekom;Telekom Operatörü;100.000.000;İstanbul;www.turktelekom.com.tr\n"
        "54;ARD Grup Holding;Yazılım;5.000.000;Ankara;https://www.ardgrup.com.tr\n"
        "114;SAMM Teknoloji;Yazılım;1.000.000;;\n",
        encoding="utf-8",
    )
    out = b.parse_bilisim500_csv(p)
    assert [r.raw["rank"] for r in out] == [1, 54, 114]
    assert out[0].city == "İstanbul" and out[0].sector == "Telekom Operatörü"
    assert out[0].website == "https://www.turktelekom.com.tr"
    assert {"bilisim500", "bilisim500-2025", "Telekom Operatörü"} <= set(out[0].tags)
    assert out[1].city == "Ankara" and out[1].raw["category"] == "Yazılım" and out[1].raw["year"] == 2025
    assert out[2].city is None and "sehir-bilinmiyor" in out[2].tags
    assert all(r.source == "bilisim500" for r in out)

    col = b.Bilisim500Collector(manual_dir=manual)
    got = list(col.collect(cities=["Ankara"]))
    # Ankara + şehri bilinmeyen kayıt kalır, İstanbul düşer
    assert [r.name for r in got] == ["ARD Grup Holding", "SAMM Teknoloji"]


def test_bilisim500_collector_without_file_is_empty(tmp_path: Path):
    assert list(b.Bilisim500Collector(manual_dir=tmp_path / "yok").collect()) == []
    bad = tmp_path / "manual"
    bad.mkdir()
    (bad / "bilisim500_2024.csv").write_text("Ciro;Kategori\n1;2\n", encoding="utf-8")
    assert list(b.Bilisim500Collector(manual_dir=bad).collect()) == []


# --- filtre ve zincir ------------------------------------------------------------------

def test_filter_by_cities(parsed):
    raws = list(parsed.values())
    names = {r.name for r in b.filter_by_cities(raws, ["konya", "ANKARA", "İstanbul"])}
    assert names == {"Firmanet A.Ş.", "Zirve Yazılım", "Netcad Yazılım", "Başarsoft", "complead", "Astera İnsan Kaynakları",
                     "Vispera Bilgi Teknolojileri Sanayi Ic ve Dis Ticaret A.S."}
    assert list(b.filter_by_cities(raws, None)) == raws
    unknown = RawCompany(name="Bilinmez", source="x", city=None)
    assert list(b.filter_by_cities([unknown], ["Konya"])) == [unknown]
    assert list(b.filter_by_cities([unknown], ["Konya"], keep_unknown=False)) == []
    assert list(b.filter_by_cities([RawCompany(name="A", source="x", city="Afyon")], ["Afyonkarahisar"]))


def test_sources_and_get_collector():
    assert b.SOURCE_NAMES == ["yasad", "tubisad", "bilisim500"]
    assert [c.name for c in b.SOURCES] == b.SOURCE_NAMES
    assert isinstance(b.get_collector("yasad", check_robots=False), b.YasadCollector)
    assert isinstance(b.get_collector("bilisim500"), b.Bilisim500Collector)
    with pytest.raises(ValueError):
        b.get_collector("bilinmeyen")


def test_association_collectors_chain_and_survive_errors():
    class Ok:
        name = "ok"

        def collect(self, cities=None):
            yield RawCompany(name="A", source="ok", city="Konya")
            yield RawCompany(name="B", source="ok", city="Ankara")

    class Boom:
        name = "boom"

        def collect(self, cities=None):
            raise RuntimeError("patladı")
            yield  # pragma: no cover

    got = list(b.AssociationCollectors(collectors=[Boom(), Ok()]).collect())
    assert [r.name for r in got] == ["A", "B"]
    # sınıf üzerinden de çağrılabilir
    got2 = list(b.AssociationCollectors.collect(None, collectors=[Ok()]))
    assert len(got2) == 2
    assert b.AssociationCollectors.name == "bilisim500"


# --- ağ katmanı (respx ile sahte sunucu) ---------------------------------------------------

def test_yasad_collector_rest_paging_and_city_filter(rest_records, tax):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/wp/v2/lokasyon.*").mock(return_value=httpx.Response(200, json=tax["lokasyon"]))
        router.get(url__regex=r".*/wp-json/wp/v2/sektor-kategorisi.*").mock(return_value=httpx.Response(200, json=tax["sektor-kategorisi"]))
        firmalar = router.get(url__regex=r".*/wp-json/wp/v2/firmalar.*").mock(side_effect=_rest_handler(rest_records, total_pages=2))
        html_list = router.get(url__regex=r".*/uyelerimiz.*").mock(return_value=httpx.Response(200, text="<html></html>"))

        col = b.YasadCollector(_fast_client(), Throttle(0))
        got = list(col.collect(cities=["Konya", "Ankara"]))

    assert firmalar.call_count == 2  # X-WP-TotalPages=2 -> iki sayfa
    assert not html_list.called  # REST çalıştıysa HTML yedeğe gidilmez
    names = sorted({r.name for r in got})
    assert names == ["Başarsoft", "Firmanet A.Ş.", "Netcad Yazılım", "Zirve Yazılım"]
    assert len(got) == 8  # iki sayfa aynı veriyi döndürdü; tekilleştirme ingest'in işi
    assert all(r.city in ("Konya", "Ankara") for r in got)


def test_yasad_collector_falls_back_to_html(listing_html, detail_html):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/.*").mock(return_value=httpx.Response(500))
        router.get("https://www.yasad.org.tr/uyelerimiz/").mock(return_value=httpx.Response(200, text=listing_html))
        router.get("https://www.yasad.org.tr/uyelerimiz/page/2/").mock(return_value=httpx.Response(404))
        detail = router.get(url__regex=r"https://www\.yasad\.org\.tr/firmalar/.*").mock(return_value=httpx.Response(200, text=detail_html))

        col = b.YasadCollector(_fast_client(), Throttle(0))
        got = list(col.collect())

    assert len(got) == 6 and detail.call_count == 6
    assert all(r.website == "http://www.architecht.com" for r in got)
    assert all(r.raw["via"] == "html" and "yasad-uye" in r.tags for r in got)
    assert got[0].name == "2 Adam Yazılım ve Teknoloji"


def test_yasad_collector_rest_without_acf_falls_back(listing_html, detail_html):
    stripped = [{"id": 1, "title": {"rendered": "X"}, "link": "https://www.yasad.org.tr/firmalar/x/"}]
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/wp/v2/(lokasyon|sektor-kategorisi).*").mock(return_value=httpx.Response(200, json=[]))
        router.get(url__regex=r".*/wp-json/wp/v2/firmalar.*").mock(side_effect=_rest_handler(stripped))
        router.get("https://www.yasad.org.tr/uyelerimiz/").mock(return_value=httpx.Response(200, text=listing_html))
        router.get(url__regex=r".*/uyelerimiz/page/.*").mock(return_value=httpx.Response(404))
        router.get(url__regex=r"https://www\.yasad\.org\.tr/firmalar/.*").mock(return_value=httpx.Response(200, text=detail_html))
        got = list(b.YasadCollector(_fast_client(), Throttle(0)).collect())
    assert len(got) == 6 and all(r.raw["via"] == "html" for r in got)


def test_yasad_collector_rest_only_no_fallback():
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/.*").mock(return_value=httpx.Response(503))
        html_list = router.get(url__regex=r".*/uyelerimiz.*").mock(return_value=httpx.Response(200, text="<html></html>"))
        got = list(b.YasadCollector(_fast_client(), Throttle(0), html_fallback=False).collect())
    assert got == [] and not html_list.called


def test_tubisad_collector(tubisad_html):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        got = list(b.TubisadCollector(_fast_client(), Throttle(0)).collect(cities=["Konya"]))
    # şehir bilinmediği için şehir filtresi hiçbirini düşürmez
    assert len(got) >= 235 and all(r.city is None for r in got)


def test_network_errors_yield_empty_without_raising():
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("bağlantı yok"))
        assert list(b.YasadCollector(_fast_client(), Throttle(0)).collect()) == []
        assert list(b.TubisadCollector(_fast_client(), Throttle(0)).collect()) == []
        assert list(b.AssociationCollectors(client=_fast_client(), throttle=Throttle(0)).collect(["Konya"])) == []


def test_robots_disallow_is_respected(tubisad_html):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
        page = router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        got = list(b.TubisadCollector(_fast_client(), Throttle(0)).collect())
    assert got == [] and not page.called


def test_robots_failure_allows_fetch(tubisad_html):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://[^/]+/robots\.txt").mock(side_effect=httpx.ConnectError("x"))
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        got = list(b.TubisadCollector(_fast_client(), Throttle(0)).collect())
    assert len(got) >= 235


def test_module_collect_uses_all_sources(rest_records, tax, tubisad_html, tmp_path: Path, monkeypatch):
    # Bilişim 500 CSV yok -> yalnızca YASAD + TÜBİSAD
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/wp/v2/lokasyon.*").mock(return_value=httpx.Response(200, json=tax["lokasyon"]))
        router.get(url__regex=r".*/wp-json/wp/v2/sektor-kategorisi.*").mock(return_value=httpx.Response(200, json=tax["sektor-kategorisi"]))
        router.get(url__regex=r".*/wp-json/wp/v2/firmalar.*").mock(side_effect=_rest_handler(rest_records))
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        monkeypatch.setattr(b.Bilisim500Collector, "_dir", lambda self: tmp_path / "manual")
        got = list(b.AssociationCollectors(client=_fast_client(), throttle=Throttle(0)).collect(["İzmir"]))
    sources = {r.source for r in got}
    assert sources == {"yasad", "tubisad"}
    yasad = [r for r in got if r.source == "yasad"]
    assert [r.name for r in yasad] == ["3S YAZILIM VE BİLİŞİM HİZMETLERİ"]


# --- gerçek ağ (varsayılan olarak atlanır: pytest -m network) ---------------------------------

@pytest.mark.network
def test_yasad_rest_live_first_page():
    with make_http_client() as client:
        r = client.get(b.YASAD_REST_URL, params={"per_page": 5, "page": 1, "_fields": b.YASAD_REST_FIELDS})
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list) and data
    out = b.parse_yasad_rest(data)
    assert out and all(x.name for x in out)
    assert any(x.website for x in out)


@pytest.mark.network
def test_tubisad_live_page():
    with make_http_client() as client:
        r = client.get(b.TUBISAD_URL)
    assert r.status_code == 200
    out = b.parse_tubisad(r.text)
    assert len(out) > 100 and all(x.website or x.linkedin_url for x in out)
