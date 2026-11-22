"""bilisim500 için ek (saldırgan) testler: bozuk girdi, Türkçe karakter, tekrar, ağ hataları, Windows yolları."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from stajbot.collectors import bilisim500 as b
from stajbot.collectors.base import RawCompany, Throttle, make_http_client

FIX = Path(__file__).parent / "fixtures" / "bilisim500"


@pytest.fixture(scope="module")
def rest_records() -> list[dict]:
    return json.loads((FIX / "yasad_firmalar_rest_sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def listing_html() -> str:
    return (FIX / "yasad_uyelerimiz_page1_trimmed.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def detail_html() -> str:
    return (FIX / "yasad_firma_detail_architecht.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tubisad_html() -> str:
    return (FIX / "tubisad_uye_listesi.html").read_text(encoding="utf-8")


def _robots_ok(router: respx.MockRouter) -> None:
    router.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))


def _rec(**acf) -> dict:
    return {"id": 1, "title": {"rendered": "Test Yazılım"}, "acf": acf}


# --- REST ayrıştırma: bozuk JSON ------------------------------------------------------

@pytest.mark.parametrize(
    "rec",
    [
        {"id": 1, "title": {"rendered": "X"}, "lokasyon": 5, "acf": {}},                 # liste değil int
        {"id": 2, "title": {"rendered": "X"}, "sektor-kategorisi": "133", "acf": {}},    # liste değil str
        {"id": 3, "title": {"rendered": "X"}, "firma-durum": {"a": 1}, "acf": {}},       # dict
        {"id": 4, "title": {"rendered": "X"}, "lokasyon": [None, "x", 3.7, "52"], "acf": {}},
        {"id": 5, "title": ["X"], "acf": {"Sirket_Unvani": "Y"}},
        {"id": 6, "title": {"rendered": "X"}, "acf": [1, 2]},
        {"id": 7, "title": {"rendered": "X"}, "slug": 5, "acf": {}},
        {"id": 8, "title": {"rendered": "X"}, "acf": {"telefon": {"a": 1}, "web_sitesi_adresi": ["x"], "Sirket_Adres": 12}},
    ],
)
def test_rest_record_malformed_fields_do_not_raise(rec):
    out = b.parse_yasad_rest([rec])
    assert len(out) == 1
    assert isinstance(out[0], RawCompany) and out[0].name


def test_rest_one_bad_record_does_not_kill_the_page(rest_records):
    bad = {"id": 99, "title": {"rendered": "Bozuk"}, "lokasyon": 5, "sektor-kategorisi": 7, "acf": {"telefon": object()}}
    out = b.parse_yasad_rest([bad] + rest_records)
    assert len(out) >= len(rest_records)


def test_rest_lokasyon_term_that_is_not_a_province_falls_back_to_address():
    g = b.resolve_city([999], "Konak / İzmir", {999: "Yurt Dışı"})
    assert g.city == "İzmir" and g.method == "province"
    g2 = b.resolve_city([999], None, {999: "Yurt Dışı"})
    assert g2.city is None


def test_rest_member_class_variants():
    assert "yasad-sinif-a" in b.parse_yasad_rest([_rec(uyelik_sinifi="A Sınıfı")])[0].tags
    assert "yasad-sinif-b" in b.parse_yasad_rest([_rec(uyelik_sinifi=" b ")])[0].tags
    tags = b.parse_yasad_rest([_rec(uyelik_sinifi="")])[0].tags
    assert not any(t.startswith("yasad-sinif-") for t in tags)
    for t in tags:
        assert " " not in t or t in ("Test Yazılım",)  # etiketlerde boşluk yok (kategori adları hariç)


def test_rest_multiple_emails_in_one_field():
    r = b.parse_yasad_rest([_rec(**{"e-mail_adresi": "INFO@X.COM; satis@x.com / bozuk@ , info@x.com"})])[0]
    assert r.emails == ["info@x.com", "satis@x.com"]  # küçük harf, tekil, geçersizler yok
    assert all(e == e.lower() for e in r.emails)


def test_rest_personal_authority_email_is_not_kept():
    r = b.parse_yasad_rest([_rec(**{"Sirket_Yetkilisi_E-Posta": "ahmet.toprak@firma.com"})])[0]
    assert r.emails == []
    r2 = b.parse_yasad_rest([_rec(**{"Sirket_Yetkilisi_E-Posta": "ik@firma.com"})])[0]
    assert r2.emails == ["ik@firma.com"]


def test_rest_founded_year_variants():
    assert b.parse_yasad_rest([_rec(kurulus_yili=1984.0)])[0].raw["founded_year"] == 1984
    assert b.parse_yasad_rest([_rec(kurulus_yili="1984")])[0].raw["founded_year"] == 1984
    assert b.parse_yasad_rest([_rec(kurulus_yili="dün")])[0].raw["founded_year"] is None
    assert b.parse_yasad_rest([_rec(kurulus_yili=84)])[0].raw["founded_year"] is None


def test_rest_duplicate_categories_not_duplicated_in_tags():
    r = b.parse_yasad_rest([{"id": 1, "title": {"rendered": "X"}, "sektor-kategorisi": [133, 133], "acf": {}}])[0]
    assert r.tags.count("Bilişim Hizmetleri Yazılımları") == 1


def test_rest_name_length_and_entities():
    long_name = "A" * 600
    r = b.parse_yasad_rest([{"id": 1, "title": {"rendered": long_name + " &amp; B"}, "acf": {}}])[0]
    assert len(r.name) <= 255
    r2 = b.parse_yasad_rest([{"id": 1, "title": {"rendered": "Ar &amp; Ge &#350;irketi"}, "acf": {}}])[0]
    assert r2.name == "Ar & Ge Şirketi"


def test_parse_is_deterministic(rest_records):
    a = b.parse_yasad_rest(rest_records)
    c = b.parse_yasad_rest(rest_records)
    assert a == c


def test_term_map_tolerates_garbage():
    assert b.term_map({"a": "b", "5": "Ankara"}) == {5: "Ankara"}
    assert b.term_map([{"id": 1.5, "name": "x"}, {"id": 2, "name": ""}, None, 7]) == {1: "x"}
    assert b.term_map("çöp") == {}


# --- şehir çıkarımı ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, city",
    [
        ("Fatih Mah. 1234 Sk. No:5", None),              # her şehirde Fatih Mahallesi var
        ("Kartal Sok. No:3", None),                      # sokak adı ilçe sayılmaz
        ("Yıldırım Caddesi No:1", None),
        ("İvedik OSB Mah. 1368. Cad.", "Ankara"),        # OSB'li yazım yine Ankara
        ("Türkiye", None),
        ("İSTANBUL / KADIKÖY", "İstanbul"),
        ("İstanbul Kadıköy", "İstanbul"),           # NFD birleşik karakter
        ("istanbul", "İstanbul"),
        ("Konya Yolu 8. km Gölbaşı", "Ankara"),
        ("Afyonkarahisar", "Afyonkarahisar"),
        ("ANKARA", "Ankara"),
        ("Esenler/İstanbul", "İstanbul"),
        ("Bornova-İZMİR", "İzmir"),
    ],
)
def test_city_from_text_edge_cases(text, city):
    assert b.city_from_text(text).city == city


def test_city_from_text_non_string_inputs():
    assert b.city_from_text(12345).city is None
    assert b.city_from_text("   ").city is None


# --- URL -----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("mailto:info@x.com", None),
        ("javascript:void(0)", None),
        ("tel:+903121234567", None),
        ("ftp://x.com", None),
        ("HTTP://HTTPS://WWW.X.COM/Yol", "https://www.x.com/Yol"),
        ("x.com.", "https://x.com"),
        ("https://x.com:8443/a", "https://x.com:8443/a"),
        ("'https://tirnakli.com'", "https://tirnakli.com"),
        ("www.şirket.com.tr", "https://www.şirket.com.tr"),
        ("http://localhost", None),
    ],
)
def test_normalize_website_rejects_non_http(raw, expected):
    assert b.normalize_website(raw) == expected


def test_site_origin_keeps_only_scheme_and_host():
    assert b.site_origin("https://x.com:8443/a?b=1#c") == "https://x.com"
    assert b.site_origin("mailto:a@b.com") is None
    assert b.site_origin(None) is None


# --- TÜBİSAD ------------------------------------------------------------------------------

def test_tubisad_dedupes_linkedin_and_skips_bad_hrefs():
    html = """<div class="HTMLContent"><table><tr>
    <td><a href="https://www.linkedin.com/company/x/">X</a></td>
    <td><a href="https://www.linkedin.com/company/x">X yine</a></td>
    <td><a href="javascript:void(0)">JS</a></td>
    <td><a href="https://x">tld yok</a></td>
    <td><a href="HTTPS://WWW.BUYUK.COM.TR/Sayfa">Büyük Harf</a></td>
    <td><a href="https://www.buyuk.com.tr/">tekrar</a></td>
    </tr></table></div>"""
    out = b.parse_tubisad(html, "http://t")
    assert len(out) == 2
    li, buyuk = out
    assert li.linkedin_url and li.website is None
    assert buyuk.website == "https://www.buyuk.com.tr" and buyuk.name == "Büyük Harf"


def test_tubisad_unicode_names_preserved(tubisad_html):
    out = b.parse_tubisad(tubisad_html)
    names = [r.name for r in out if r.raw["name_source"] == "span"]
    assert any("İ" in n or "Ş" in n or "Ğ" in n for n in names)
    assert all(len(r.name) <= 255 for r in out)


def test_tubisad_garbage_html_is_empty():
    assert b.parse_tubisad("<<<>>>&&& <table><td><a>x</a></td>") == []
    assert b.parse_tubisad("") == []


# --- Bilişim 500 CSV --------------------------------------------------------------------------

def test_csv_windows_1254_encoding_from_excel(tmp_path: Path):
    """Türkçe Excel 'CSV (noktalı virgülle ayrılmış)' çıktısı cp1254 kodlu olur."""
    p = tmp_path / "bilisim500_2025.csv"
    p.write_bytes("Sıra;Şirket;Kategori;Ciro;Şehir\n1;Şirket Ağ A.Ş.;Yazılım;1.000;İstanbul\n".encode("cp1254"))
    out = b.parse_bilisim500_csv(p)
    assert len(out) == 1
    assert out[0].name == "Şirket Ağ A.Ş." and out[0].city == "İstanbul" and out[0].raw["year"] == 2025


def test_csv_with_bom_commas_and_quoted_newlines(tmp_path: Path):
    p = tmp_path / "bilisim500-2024.csv"
    p.write_text(
        '﻿Sıra,Şirket,Kategori,Ciro\n1,"Virgül, Ltd.",Yazılım,"1,000"\n2,"Çok\nSatırlı A.Ş.",Donanım,5\n\n',
        encoding="utf-8",
    )
    out = b.parse_bilisim500_csv(p)
    assert [r.name for r in out] == ["Virgül, Ltd.", "Çok Satırlı A.Ş."]
    assert out[0].raw["rank"] == 1 and out[0].raw["revenue"] == "1,000"
    assert "bilisim500-2024" in out[0].tags


def test_csv_tab_delimited_and_alternate_headers(tmp_path: Path):
    p = tmp_path / "bilisim500.csv"
    p.write_text("No\tFirma Adı\tSektör\tİl\tWeb Sitesi\n3\tÖrnek\tYazılım\tKonya\twww.ornek.com\n", encoding="utf-8")
    out = b.parse_bilisim500_csv(p, year=2023)
    assert len(out) == 1 and out[0].raw["rank"] == 3 and out[0].city == "Konya"
    assert out[0].website == "https://www.ornek.com" and "bilisim500-2023" in out[0].tags


def test_csv_malformed_rows(tmp_path: Path):
    p = tmp_path / "bilisim500_2025.csv"
    p.write_text("Sıra;Şirket;Kategori\n;;\n1\n2;Kısa\n3;Tam;Yazılım;Fazla;Sütun\n", encoding="utf-8")
    out = b.parse_bilisim500_csv(p)
    assert [r.name for r in out] == ["Kısa", "Tam"]


def test_csv_empty_and_header_only(tmp_path: Path):
    p = tmp_path / "bilisim500_a.csv"
    p.write_text("", encoding="utf-8")
    assert b.parse_bilisim500_csv(p) == []
    p.write_text("Sıra;Şirket\n", encoding="utf-8")
    assert b.parse_bilisim500_csv(p) == []


def test_csv_source_url_is_valid_file_uri_on_windows_paths(tmp_path: Path):
    p = tmp_path / "bilisim500_2025.csv"
    p.write_text("Şirket\nA\n", encoding="utf-8")
    out = b.parse_bilisim500_csv(str(p).replace("/", "\\"))
    assert out[0].source_url.startswith("file:///") and "\\" not in out[0].source_url


def test_csv_collector_runs_twice_identically_and_survives_bad_file(tmp_path: Path):
    manual = tmp_path / "manual"
    manual.mkdir()
    (manual / "bilisim500_2025.csv").write_text("Şirket;Şehir\nA;Ankara\nB;Konya\n", encoding="utf-8")
    (manual / "bilisim500_bozuk.csv").write_bytes(b"\xff\xfe\x00\x00garbage")
    col = b.Bilisim500Collector(manual_dir=str(manual))
    first = list(col.collect())
    second = list(col.collect())
    assert [r.name for r in first] == [r.name for r in second]
    assert {r.name for r in first} >= {"A", "B"}
    assert list(b.Bilisim500Collector(manual_dir=manual).collect(cities=["Konya"]))[0].city == "Konya"


def test_csv_collector_directory_is_a_file(tmp_path: Path):
    f = tmp_path / "dosya"
    f.write_text("x", encoding="utf-8")
    assert list(b.Bilisim500Collector(manual_dir=f).collect()) == []


# --- ağ katmanı -----------------------------------------------------------------------------------

def test_rest_non_json_200_falls_back_to_html(listing_html, detail_html):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/.*").mock(return_value=httpx.Response(200, text="<html>bakım</html>"))
        router.get("https://www.yasad.org.tr/uyelerimiz/").mock(return_value=httpx.Response(200, text=listing_html))
        router.get(url__regex=r".*/uyelerimiz/page/.*").mock(return_value=httpx.Response(404))
        router.get(url__regex=r"https://www\.yasad\.org\.tr/firmalar/.*").mock(return_value=httpx.Response(200, text=detail_html))
        got = list(b.YasadCollector(make_http_client(), Throttle(0)).collect())
    assert len(got) == 6 and all(r.raw["via"] == "html" for r in got)


def test_rest_error_object_falls_back_to_html(listing_html):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/.*").mock(return_value=httpx.Response(200, json={"code": "rest_no_route"}))
        router.get("https://www.yasad.org.tr/uyelerimiz/").mock(return_value=httpx.Response(200, text=listing_html))
        router.get(url__regex=r".*/uyelerimiz/page/.*").mock(return_value=httpx.Response(404))
        router.get(url__regex=r"https://www\.yasad\.org\.tr/firmalar/.*").mock(side_effect=httpx.ReadTimeout("zaman aşımı"))
        got = list(b.YasadCollector(make_http_client(), Throttle(0)).collect())
    # detay sayfaları alınamasa da kartlar üretilir
    assert len(got) == 6 and all(r.website is None and r.city is None for r in got)


def test_rest_second_page_failure_keeps_first_page(rest_records):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=rest_records, headers={"X-WP-TotalPages": "bozuk"})
        raise httpx.ConnectError("koptu")

    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/wp/v2/(lokasyon|sektor-kategorisi).*").mock(return_value=httpx.Response(500))
        router.get(url__regex=r".*/wp-json/wp/v2/firmalar.*").mock(side_effect=handler)
        html_list = router.get(url__regex=r".*/uyelerimiz.*").mock(return_value=httpx.Response(200, text="<html></html>"))
        got = list(b.YasadCollector(make_http_client(), Throttle(0)).collect())
    assert len(got) == len(rest_records)  # TotalPages bozuk -> tek sayfa
    assert not html_list.called
    assert calls["n"] == 1


def test_rest_total_pages_huge_is_bounded(rest_records):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rest_records, headers={"X-WP-TotalPages": "99999"})

    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/wp/v2/(lokasyon|sektor-kategorisi).*").mock(return_value=httpx.Response(200, json=[]))
        firmalar = router.get(url__regex=r".*/wp-json/wp/v2/firmalar.*").mock(side_effect=handler)
        got = list(b.YasadCollector(make_http_client(), Throttle(0), max_pages=3).collect())
    assert firmalar.call_count == 3 and len(got) == 3 * len(rest_records)


def test_html_listing_self_referencing_next_does_not_loop(listing_html):
    looped = listing_html.replace("/uyelerimiz/page/2/", "/uyelerimiz/")
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/.*").mock(return_value=httpx.Response(500))
        lst = router.get("https://www.yasad.org.tr/uyelerimiz/").mock(return_value=httpx.Response(200, text=looped))
        router.get(url__regex=r"https://www\.yasad\.org\.tr/firmalar/.*").mock(return_value=httpx.Response(404))
        got = list(b.YasadCollector(make_http_client(), Throttle(0)).collect())
    assert lst.call_count == 1 and len(got) == 6


def test_html_listing_cycle_between_two_pages_stops(listing_html):
    page1 = listing_html
    page2 = listing_html.replace("/uyelerimiz/page/2/", "/uyelerimiz/")
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(url__regex=r".*/wp-json/.*").mock(return_value=httpx.Response(500))
        p1 = router.get("https://www.yasad.org.tr/uyelerimiz/").mock(return_value=httpx.Response(200, text=page1))
        p2 = router.get("https://www.yasad.org.tr/uyelerimiz/page/2/").mock(return_value=httpx.Response(200, text=page2))
        router.get(url__regex=r"https://www\.yasad\.org\.tr/firmalar/.*").mock(return_value=httpx.Response(404))
        got = list(b.YasadCollector(make_http_client(), Throttle(0)).collect())
    assert p1.call_count == 1 and p2.call_count == 1
    assert len(got) == 6  # aynı detay URL'leri ikinci kez üretilmez


def test_tubisad_page_without_table_logs_and_yields_nothing(caplog):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text="<html><body><p>Bakımdayız</p></body></html>"))
        with caplog.at_level("WARNING", logger="stajbot"):
            got = list(b.TubisadCollector(make_http_client(), Throttle(0)).collect())
    assert got == []
    assert any("TÜBİSAD" in m for m in caplog.messages)


def test_http_500_and_timeout_yield_empty():
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(500))
        assert list(b.TubisadCollector(make_http_client(), Throttle(0)).collect()) == []
        router.get(b.TUBISAD_URL).mock(side_effect=httpx.ReadTimeout("yavaş"))
        assert list(b.TubisadCollector(make_http_client(), Throttle(0)).collect()) == []


def test_shared_client_is_not_closed_by_collector(tubisad_html):
    client = make_http_client()
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        col = b.TubisadCollector(client, Throttle(0))
        first = list(col.collect())
        second = list(col.collect())  # aynı örnek ikinci kez çalışır
    assert not client.is_closed
    assert len(first) == len(second) >= 235
    client.close()


def test_owned_client_is_closed_after_collect(tubisad_html):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        col = b.TubisadCollector(None, Throttle(0))
        got = list(col.collect())
        assert got
        assert col._client is None  # sahip olunan istemci kapatıldı ve bırakıldı
        got2 = list(col.collect())  # yeniden açılır
        assert len(got2) == len(got)


def test_early_stop_of_generator_is_safe(tubisad_html):
    with respx.mock(assert_all_called=False) as router:
        _robots_ok(router)
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        gen = b.TubisadCollector(make_http_client(), Throttle(0)).collect()
        first = next(gen)
        gen.close()
    assert isinstance(first, RawCompany)


def test_association_partial_source_keeps_partial_results():
    class Half:
        name = "half"

        def collect(self, cities=None):
            yield RawCompany(name="A", source="half")
            raise RuntimeError("yarıda")

    class Ok:
        name = "ok"

        def collect(self, cities=None):
            yield RawCompany(name="B", source="ok")

    got = list(b.AssociationCollectors(collectors=[Half(), Ok()]).collect(["Konya"]))
    assert [r.name for r in got] == ["A", "B"]


def test_association_instances_with_explicit_client_do_not_close_it():
    client = make_http_client()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("yok"))
        got = list(b.AssociationCollectors(client=client, throttle=Throttle(0)).collect())
    assert got == [] and not client.is_closed
    client.close()


def test_robots_html_body_is_treated_as_allow(tubisad_html):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(200, text="<html>404</html>"))
        router.get(b.TUBISAD_URL).mock(return_value=httpx.Response(200, text=tubisad_html))
        got = list(b.TubisadCollector(make_http_client(), Throttle(0)).collect())
    assert len(got) >= 235


def test_robots_specific_path_disallow():
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://[^/]+/robots\.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /wp-json/\n")
        )
        rest = router.get(url__regex=r".*/wp-json/.*").mock(return_value=httpx.Response(200, json=[]))
        lst = router.get(url__regex=r".*/uyelerimiz.*").mock(return_value=httpx.Response(200, text="<html></html>"))
        got = list(b.YasadCollector(make_http_client(), Throttle(0)).collect())
    assert got == [] and not rest.called and lst.called


def test_get_collector_passes_kwargs_only_where_supported():
    col = b.get_collector("tubisad", check_robots=False, url="http://x")
    assert isinstance(col, b.TubisadCollector) and col.url == "http://x"
    with pytest.raises(ValueError):
        b.get_collector("")


def test_filter_by_cities_tolerates_blank_and_alias_entries():
    raws = [RawCompany(name="A", source="x", city="ISTANBUL"), RawCompany(name="B", source="x", city="Afyon"),
            RawCompany(name="C", source="x", city="Kocaeli")]
    got = list(b.filter_by_cities(raws, ["", "  ", "istanbul", "afyonkarahisar"]))
    assert [r.name for r in got] == ["A", "B"]
    assert list(b.filter_by_cities([], ["Konya"])) == []
    assert list(b.filter_by_cities(raws, [])) == raws


def test_turkish_log_messages_for_missing_csv(tmp_path: Path, caplog):
    with caplog.at_level("INFO", logger="stajbot"):
        list(b.Bilisim500Collector(manual_dir=tmp_path / "yok").collect())
    assert any("Bilişim 500" in m for m in caplog.messages)


def test_module_has_no_print():
    src = Path(b.__file__).read_text(encoding="utf-8")
    import re

    assert not re.search(r"^\s*print\(", src, re.M)
