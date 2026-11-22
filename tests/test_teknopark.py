"""Teknopark collector testleri (fikstür tabanlı; ağ testleri `network` işaretli)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from stajbot.collectors import teknopark as tp
from stajbot.collectors.base import RawCompany
from stajbot.enums import TARGET_CITIES, ascii_fold

FIXTURES = Path(__file__).parent / "fixtures" / "teknopark"


@pytest.fixture(scope="module")
def config():
    return tp.load_config()


@pytest.fixture(scope="module")
def entries(config):
    return {e["slug"]: e for e in config["technoparks"]}


def _read_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _read_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# YAML yapılandırması
# ---------------------------------------------------------------------------


def test_config_loads_with_required_keys(config, entries):
    assert len(config["technoparks"]) >= 19
    assert config["defaults"]["max_detail_pages"] == 150
    slugs = [e["slug"] for e in config["technoparks"]]
    assert len(slugs) == len(set(slugs)), "slug'lar tekil olmalı"
    targets = {ascii_fold(c) for c in TARGET_CITIES}
    for e in config["technoparks"]:
        for key in tp.REQUIRED_KEYS:
            assert e.get(key), f"{e.get('slug')}: {key} eksik"
        assert e["kind"] in tp.VALID_KINDS
        assert ascii_fold(e["city"]) in targets, f"{e['slug']}: hedef dışı şehir {e['city']}"
        assert e["list_url"].startswith("http")
        if e["kind"] == "html":
            assert e["selectors"].get("item") and e["selectors"].get("name"), e["slug"]
        if e["kind"] in ("json", "inline_json"):
            assert "items" in e["json_keys"] and "name" in e["json_keys"], e["slug"]
        if e.get("detail_page"):
            assert e.get("detail_selectors") or e.get("detail_json_keys"), e["slug"]
    # her hedef şehir için en az bir teknopark
    covered = {ascii_fold(e["city"]) for e in config["technoparks"]}
    assert covered == targets


def test_config_marks_teknopark_istanbul_unsupported(entries):
    assert entries["teknopark-istanbul"]["kind"] == "js_unsupported"


def test_load_config_skips_incomplete_entries(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(
        "technoparks:\n  - slug: a\n    name: A\n  - slug: b\n    name: B\n    city: Konya\n    kind: html\n    list_url: https://x.test/\n",
        encoding="utf-8",
    )
    cfg = tp.load_config(p)
    assert [e["slug"] for e in cfg["technoparks"]] == ["b"]
    assert cfg["technoparks"][0]["max_detail_pages"] == 150


# ---------------------------------------------------------------------------
# HTML liste fikstürleri
# ---------------------------------------------------------------------------

# (slug, fikstür, en az kayıt, web sitesi bekleniyor mu, sektör bekleniyor mu)
HTML_CASES = [
    ("bilkent-cyberpark", "bilkent-cyberpark.html", 20, False, False),
    ("hacettepe-teknokent", "hacettepe-teknokent.html", 40, False, True),
    ("aso-teknopark", "aso-teknopark.html", 60, True, False),
    ("itu-ari-teknokent", "itu-ari-teknokent.html", 24, False, True),
    ("yildiz-teknopark", "yildiz-teknopark.html", 36, True, False),
    ("bogazici-teknopark", "bogazici-teknopark.html", 6, False, True),
    ("entertech-istanbul-teknokent", "entertech-istanbul-teknokent.html", 6, False, False),
    ("teknopark-izmir", "teknopark-izmir.html", 6, True, True),
    ("depark", "depark.html", 6, True, True),
    ("izmir-bilimpark", "izmir-bilimpark.html", 6, True, True),
    ("atap-eskisehir", "atap-eskisehir.html", 6, True, True),
    ("ulutek", "ulutek.html", 40, False, False),
    ("antalya-teknokent", "antalya-teknokent.html", 20, False, False),
    ("mersin-technoscope", "mersin-technoscope.html", 12, False, False),
    ("zafer-teknopark", "zafer-teknopark.html", 6, True, False),
]


@pytest.mark.parametrize("slug,fixture,min_count,expect_web,expect_sector", HTML_CASES, ids=[c[0] for c in HTML_CASES])
def test_parse_list_html_fixtures(entries, slug, fixture, min_count, expect_web, expect_sector):
    entry = entries[slug]
    companies = tp.parse_list_html(_read_bytes(fixture), entry)
    assert len(companies) >= min_count
    for c in companies:
        assert isinstance(c, RawCompany)
        assert c.name and c.name.strip()
        assert c.source == "teknopark"
        assert c.is_teknopark is True
        assert c.teknopark_name == entry["name"]
        assert c.city == entry["city"]
        assert c.source_url == entry["list_url"]
        assert c.raw["teknopark_slug"] == slug
        if c.website:
            assert c.website.startswith("http")
            assert "." in c.website.split("//", 1)[1]
    if expect_web:
        assert sum(1 for c in companies if c.website) >= len(companies) // 2
    if expect_sector:
        assert sum(1 for c in companies if c.sector) >= len(companies) // 2


def test_parse_specific_values(entries):
    aso = tp.parse_list_html(_read_bytes("aso-teknopark.html"), entries["aso-teknopark"])
    first = aso[0]
    assert first.name.startswith("2S SOFT")
    assert first.website == "http://www.goonex.com.tr/"

    izmir = tp.parse_list_html(_read_bytes("teknopark-izmir.html"), entries["teknopark-izmir"])
    assert izmir[0].website == "http://www.3faktoriyel.com"
    assert izmir[0].sector == "Yazılım"

    bilim = tp.parse_list_html(_read_bytes("izmir-bilimpark.html"), entries["izmir-bilimpark"])
    assert bilim[0].emails == ["info@networks.com.tr"]
    assert bilim[0].sector == "Yazılım"
    assert bilim[0].website == "https://networks.com.tr/"

    atap = tp.parse_list_html(_read_bytes("atap-eskisehir.html"), entries["atap-eskisehir"])
    assert atap[0].name == "2N İnovasyon"
    assert atap[0].sector == "Yazılım"
    assert atap[0].phone == "+90 222 335 90 37"
    assert atap[0].website == "http://www.2ninovasyon.com"
    assert atap[0].raw["legal_name"] == "2N İnovasyon Ar-Ge Yazılım Tic. A.Ş."
    assert atap[0].raw["detail_link"] == "https://www.atap.com.tr/Portal/FirmaDetay/19/2n-inovasyon"
    # "http://www." gibi boş web adresleri süzülür
    assert all(c.website is None or tp.domain_of(c.website) for c in atap)

    ari = tp.parse_list_html(_read_bytes("itu-ari-teknokent.html"), entries["itu-ari-teknokent"])
    assert ari[0].raw["detail_id"] == "48975"
    assert "Yazılım" in ari[0].sector

    bog = tp.parse_list_html(_read_bytes("bogazici-teknopark.html"), entries["bogazici-teknopark"])
    assert bog[0].name == "DEVS DANIŞMANLIK"
    assert bog[0].sector == "AR-GE"
    assert bog[0].raw["detail_link"].startswith("https://bogaziciteknopark.com/firmalarimiz/")

    zafer = tp.parse_list_html(_read_bytes("zafer-teknopark.html"), entries["zafer-teknopark"])
    assert zafer[0].name == "3D Zeka Yetenek"
    assert zafer[0].website == "https://neurosound.com.tr/"
    assert zafer[0].raw["legal_name"].startswith("3D Zeka Yetenek Dikkat")

    hac = tp.parse_list_html(_read_bytes("hacettepe-teknokent.html"), entries["hacettepe-teknokent"])
    # sektör, sayfa breadcrumb'ından (context_selectors) gelir
    assert hac[0].sector == "BİLGİSAYAR VE İLETİŞİM TEKNOLOJİLERİ"
    assert hac[0].raw["detail_link"] == "https://www.hacettepeteknokent.com.tr/tr/firma/adeo_cyber_security-2976"


def test_context_fills_sector_and_building(entries):
    mersin = tp.parse_list_html(_read_bytes("mersin-technoscope.html"), entries["mersin-technoscope"], context={"sector": "Bilişim"})
    assert mersin and all(c.sector == "Bilişim" for c in mersin)
    assert mersin[0].raw["detail_link"] == "https://www.technoscope.com.tr/aek/"

    antalya = tp.parse_list_html(_read_bytes("antalya-teknokent.html"), entries["antalya-teknokent"], context={"building": "AR-GE 1 Binası"})
    assert antalya and all("AR-GE 1 Binası" in c.tags for c in antalya)
    assert antalya[0].raw["building"] == "AR-GE 1 Binası"
    assert antalya[0].sector is None


# ---------------------------------------------------------------------------
# Alt bağlantılar (indeks sayfaları)
# ---------------------------------------------------------------------------


def test_extract_sub_links_hacettepe(entries):
    links = tp.extract_sub_links(_read_bytes("hacettepe-teknokent-categories.html"), entries["hacettepe-teknokent"])
    assert len(links) == 15
    urls = [u for u, _ in links]
    assert len(set(urls)) == 15
    assert urls[0] == "https://www.hacettepeteknokent.com.tr/tr/firma_rehberi/bilgisayar_ve_iletisim_teknolojileri-16"
    assert links[0][1] == "BİLGİSAYAR VE İLETİŞİM TEKNOLOJİLERİ"
    assert all(label for _, label in links)


def test_extract_sub_links_antalya_buildings(entries):
    links = tp.extract_sub_links(_read_bytes("antalya-teknokent-buildings.html"), entries["antalya-teknokent"])
    assert len(links) == 9
    assert links[0] == ("https://www.antalyateknokent.com.tr/tr/ar-ge-1-binasi", "AR-GE 1 Binası")
    assert any(u.endswith("girisimcilik-ve-kulucka-merkezi1") for u, _ in links)


def test_extract_sub_links_mersin_sectors(entries):
    links = tp.extract_sub_links(_read_bytes("mersin-technoscope-sectors.html"), entries["mersin-technoscope"])
    assert len(links) == 11
    assert links[0] == ("https://www.technoscope.com.tr/menu/bilisim/", "Bilişim")


# ---------------------------------------------------------------------------
# JSON kaynaklar
# ---------------------------------------------------------------------------


def test_parse_json_odtu(entries):
    data = json.loads(_read_text("odtu-teknokent.json"))
    companies = tp.parse_json(data, entries["odtu-teknokent"])
    assert len(companies) == 392
    assert companies[0].name.startswith("3DTİM")
    assert companies[0].website == "http://www.beevision.ai"
    assert sum(1 for c in companies if c.website) >= 300
    assert all(c.city == "Ankara" and c.is_teknopark for c in companies)


def test_parse_json_gazi(entries):
    data = json.loads(_read_text("gazi-teknopark.json"))
    companies = tp.parse_json(data, entries["gazi-teknopark"])
    assert len(companies) >= 5
    c = companies[0]
    assert c.name.startswith("1İf")
    assert c.website == "http://www.1if.com.tr/"
    assert c.emails == [] and c.phone is None  # 'Yok' boş sayılır
    assert c.description and "1IF" in c.description


def test_parse_json_konya_with_sector_lookup(entries):
    data = tp._loads_lenient(_read_text("konya-teknokent.json"))
    companies = tp.parse_json(data, entries["konya-teknokent"])
    assert len(companies) == 208
    c = companies[0]
    assert c.name.startswith("1215 YAZILIM")
    assert c.sector == "Yazılım"  # kategori id -> ad
    assert c.phone == "+90 332 262 02 02"
    assert c.website is None
    assert sum(1 for x in companies if x.website) > 50


def test_loads_lenient_accepts_control_chars():
    raw = '{"companies": [{"name": "A\x01B"}]}'
    with pytest.raises(ValueError):
        json.loads(raw)
    assert tp._loads_lenient(raw)["companies"][0]["name"] == "A\x01B"


def test_parse_json_sakarya_dataset(entries):
    data = json.loads(_read_text("sakarya-teknokent.json"))
    companies = tp.parse_json(data, entries["sakarya-teknokent"])
    assert len(companies) == 137
    with_web = [c for c in companies if c.website]
    assert len(with_web) >= 50
    assert any(c.emails for c in companies)
    assert any(c.sector == "Yazılım" for c in companies)
    assert any(c.employee_count for c in companies)


def test_extract_inline_json_sakarya(entries):
    html = _read_text("sakarya-teknokent.html")
    data = tp.extract_inline_json(html, entries["sakarya-teknokent"])
    assert isinstance(data, list) and len(data) >= 5
    companies = tp.parse_json(data, entries["sakarya-teknokent"])
    assert companies[0].name == "ABDULLAH KAYAR"
    assert companies[0].sector == "Yazılım"
    assert companies[0].emails == ["ensarrocakci@gmail.com"]


def test_extract_inline_json_missing_marker(entries):
    with pytest.raises(ValueError):
        tp.extract_inline_json("<html></html>", entries["sakarya-teknokent"])


# ---------------------------------------------------------------------------
# Detay sayfaları
# ---------------------------------------------------------------------------


def test_apply_detail_html_entertech(entries):
    entry = entries["entertech-istanbul-teknokent"]
    c = tp.parse_list_html(_read_bytes("entertech-istanbul-teknokent.html"), entry)[0]
    assert c.raw["detail_id"] == "80086"
    tp.apply_detail_html(c, _read_bytes("entertech-istanbul-teknokent-detail.html"), entry)
    assert c.sector == "Fintek (Finansal Teknolojiler)"
    assert c.emails == ["info@dgpays.com"]
    assert c.phone == "0 212 290 40 10"
    assert c.website == "http://www.dgpays.com"


def test_apply_detail_json_ari(entries):
    entry = entries["itu-ari-teknokent"]
    c = tp.parse_list_html(_read_bytes("itu-ari-teknokent.html"), entry)[0]
    c.sector = None
    tp.apply_detail_json(c, json.loads(_read_text("itu-ari-teknokent-detail.html")), entry)
    assert c.sector == "Bilgisayar ve İletişim Teknolojileri, Yazılım"
    assert c.website is None  # kaynakta null
    assert c.description and c.description.startswith("25 Proje Teknoloji")
    assert "<p>" not in c.description


def test_detail_does_not_overwrite_existing(entries):
    entry = entries["entertech-istanbul-teknokent"]
    c = RawCompany(name="X", source="teknopark", website="https://zaten.var", sector="Mevcut")
    tp.apply_detail_html(c, _read_bytes("entertech-istanbul-teknokent-detail.html"), entry)
    assert c.website == "https://zaten.var"
    assert c.sector == "Mevcut"
    assert c.emails == ["info@dgpays.com"]


# ---------------------------------------------------------------------------
# Sağlamlık
# ---------------------------------------------------------------------------


def test_parse_list_html_never_raises_on_bad_selectors(entries, caplog):
    entry = dict(entries["aso-teknopark"], selectors={"item": "a.af-card", "name": "span.yok", "website": "[[[", "sector": "@data-x"})
    with caplog.at_level(logging.INFO, logger="stajbot"):
        companies = tp.parse_list_html(_read_bytes("aso-teknopark.html"), entry)
    assert companies == []
    assert any("atlandı" in r.getMessage() for r in caplog.records)

    assert tp.parse_list_html(b"not html at all", entries["aso-teknopark"]) == []
    assert tp.parse_list_html(b"<div class='af-card'></div>", dict(entries["aso-teknopark"], selectors={})) == []
    assert tp.parse_json({"foo": 1}, entries["odtu-teknokent"]) == []
    assert tp.parse_json([{"url": "http://x.com"}], entries["odtu-teknokent"]) == []  # adsız kayıt


def test_field_spec_variants():
    html = "<div class='it' data-n='Attr Ad' title='T'><h3>Baslik</h3><span>a</span><span>b</span><p>Sektör: Yazılım</p><a href='/x'>l</a></div>"
    soup = tp._soup(html)
    item = soup.select_one("div.it")
    assert tp._extract(item, "h3") == "Baslik"
    assert tp._extract(item, "@data-n") == "Attr Ad"
    assert tp._extract(item, "a@href") == "/x"
    assert tp._extract(item, ["h9", "@title"]) == "T"
    assert tp._extract(item, {"sel": "span", "index": -1}) == "b"
    assert tp._extract(item, {"sel": "p", "regex": r"Sektör:\s*(.+)"}) == "Yazılım"
    assert tp._extract(item, "h9") is None
    assert tp._extract(item, "") is None


def test_select_entries_city_filter_is_accent_insensitive(config):
    allv = config["technoparks"]
    ist = tp.select_entries(allv, cities=["istanbul"])
    assert ist and all(e["city"] == "İstanbul" for e in ist)
    konya = tp.select_entries(allv, cities=["KONYA"])
    assert [e["slug"] for e in konya] == ["konya-teknokent"]
    both = tp.select_entries(allv, cities=["İzmir", "Eskisehir"])
    assert {e["city"] for e in both} == {"İzmir", "Eskişehir"}
    assert tp.select_entries(allv, cities=["Trabzon"]) == []
    assert [e["slug"] for e in tp.select_entries(allv, slugs=["depark", "ULUTEK"])] == ["depark", "ulutek"]
    assert tp.select_entries(allv, cities=["Bursa"], slugs=["depark"]) == []


def test_dedupe_merges_duplicates(entries):
    a = RawCompany(name="Örnek Yazılım A.Ş.", source="teknopark", sector="Yazılım")
    b = RawCompany(name="ÖRNEK YAZILIM LTD. ŞTİ.", source="teknopark", website="https://ornek.com", sector="Elektronik", emails=["a@ornek.com"])
    out = tp.TeknoparkCollector._dedupe([a, b], entries["hacettepe-teknokent"])
    assert len(out) == 1
    assert out[0].website == "https://ornek.com"
    assert out[0].sector == "Yazılım"
    assert out[0].raw["sectors"] == ["Yazılım", "Elektronik"]
    assert out[0].emails == ["a@ornek.com"]


# ---------------------------------------------------------------------------
# collect() akışı — HTTP respx ile taklit edilir
# ---------------------------------------------------------------------------


def _collector(**kw) -> tp.TeknoparkCollector:
    return tp.TeknoparkCollector(client=httpx.Client(follow_redirects=True), delay=0, **kw)


@pytest.mark.respx(assert_all_called=False)
def test_collect_js_unsupported_logs_turkish_and_yields_nothing(respx_mock, caplog):
    col = _collector(slugs=["teknopark-istanbul"])
    with caplog.at_level(logging.WARNING, logger="stajbot"):
        out = list(col.collect())
    assert out == []
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "Teknopark İstanbul" in msgs and "CSV" in msgs


@pytest.mark.respx(assert_all_called=False)
def test_collect_paginated_html_with_json_details(respx_mock, entries):
    entry = entries["itu-ari-teknokent"]
    base = "https://www.ariteknokent.com.tr"
    respx_mock.get(f"{base}/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get(entry["list_url"], params__contains={"page": "2"}).mock(return_value=httpx.Response(200, text="<html><body></body></html>"))
    respx_mock.get(entry["list_url"]).mock(return_value=httpx.Response(200, content=_read_bytes("itu-ari-teknokent.html")))
    detail = respx_mock.get(f"{base}/tr/getCompanyInformations").mock(
        return_value=httpx.Response(200, text=_read_text("itu-ari-teknokent-detail.html"))
    )
    col = _collector(slugs=["itu-ari-teknokent"])
    out = list(col.collect(cities=["İstanbul"]))
    assert len(out) == 24
    assert col.stats["itu-ari-teknokent"] == 24
    assert detail.called
    assert detail.calls[0].request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert "rowID=48975" in str(detail.calls[0].request.url)
    assert out[0].description and out[0].description.startswith("25 Proje")
    # şehir süzgeci: başka şehir istenirse hiçbir şey gelmez
    assert list(_collector(slugs=["itu-ari-teknokent"]).collect(cities=["Konya"])) == []


@pytest.mark.respx(assert_all_called=False)
def test_collect_index_with_sub_links_and_sub_pagination(respx_mock):
    entry = {
        "slug": "test-park", "name": "Test Park", "city": "Mersin", "kind": "html",
        "list_url": "https://park.test/firmalar/", "pagination": "none",
        "sub_links": "ul.sektor a", "sub_link_field": "sector",
        "sub_pagination": "page_param:SayfaNo", "page_start": 2, "page_size": 2, "max_pages": 10,
        "selectors": {"item": "div.f", "name": "h4", "detail_link": "a@href"},
        "detail_page": True, "detail_kind": "html", "detail_selectors": {"website": "a.web@href"},
        "max_detail_pages": 1,
    }
    respx_mock.get("https://park.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /gizli\n"))
    respx_mock.get("https://park.test/firmalar/").mock(return_value=httpx.Response(200, text="<ul class='sektor'><li><a href='/menu/bilisim/'>Bilişim</a></li><li><a href='/menu/gida/'>Gıda</a></li><li><a href='/menu/bilisim/'>Bilişim</a></li></ul>"))
    p1 = "<div class='f'><h4>Alfa Yazılım</h4><a href='/alfa/'>d</a></div><div class='f'><h4>Beta Bilişim</h4><a href='/beta/'>d</a></div>"
    p2 = "<div class='f'><h4>Gama Teknoloji</h4><a href='/gama/'>d</a></div>"
    respx_mock.get("https://park.test/menu/bilisim/", params__contains={"SayfaNo": "2"}).mock(return_value=httpx.Response(200, text=p2))
    respx_mock.get("https://park.test/menu/bilisim/").mock(return_value=httpx.Response(200, text=p1))
    respx_mock.get("https://park.test/menu/gida/").mock(return_value=httpx.Response(200, text="<div class='f'><h4>Delta Gıda</h4><a href='/delta/'>d</a></div><div class='f'><h4>Beta Bilişim</h4><a href='/beta/'>d</a></div>"))
    detail = respx_mock.get(url__regex=r"https://park\.test/(alfa|beta|gama|delta)/").mock(
        return_value=httpx.Response(200, text="<a class='web' href='https://alfa.com.tr'>site</a>")
    )

    col = tp.TeknoparkCollector(client=httpx.Client(), delay=0)
    col.entries_override = None
    companies = col._collect_entry(entry, httpx.Client(), tp.Throttle(0))
    names = sorted(c.name for c in companies)
    assert names == ["Alfa Yazılım", "Beta Bilişim", "Delta Gıda", "Gama Teknoloji"]
    by_name = {c.name: c for c in companies}
    assert by_name["Alfa Yazılım"].sector == "Bilişim"
    assert by_name["Gama Teknoloji"].sector == "Bilişim"  # 2. sayfa
    assert by_name["Delta Gıda"].sector == "Gıda"
    assert by_name["Beta Bilişim"].raw["sectors"] == ["Bilişim", "Gıda"]  # iki sektörde göründü
    assert by_name["Alfa Yazılım"].raw["detail_link"] == "https://park.test/alfa/"
    # max_detail_pages=1: yalnızca bir detay sayfası çekilir
    assert detail.call_count == 1
    assert sum(1 for c in companies if c.website) == 1


@pytest.mark.respx(assert_all_called=False)
def test_collect_respects_robots_and_survives_errors(respx_mock, caplog):
    entry = {
        "slug": "kapali", "name": "Kapalı Park", "city": "Bursa", "kind": "html",
        "list_url": "https://kapali.test/firmalar", "pagination": "none",
        "selectors": {"item": "div", "name": "h4"},
    }
    respx_mock.get("https://kapali.test/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    listing = respx_mock.get("https://kapali.test/firmalar").mock(return_value=httpx.Response(200, text="<div><h4>A</h4></div>"))
    col = tp.TeknoparkCollector(client=httpx.Client(), delay=0)
    with caplog.at_level(logging.WARNING, logger="stajbot"):
        assert col._collect_entry(entry, httpx.Client(), tp.Throttle(0)) == []
    assert not listing.called
    assert any("robots.txt" in r.getMessage() for r in caplog.records)

    # ağ hatası: boş döner, exception yok
    entry2 = dict(entry, slug="hata", name="Hata Park", list_url="https://hata.test/firmalar")
    respx_mock.get("https://hata.test/robots.txt").mock(side_effect=httpx.ConnectError("dns"))
    respx_mock.get("https://hata.test/firmalar").mock(side_effect=httpx.ConnectError("dns"))
    assert col._collect_entry(entry2, httpx.Client(), tp.Throttle(0)) == []

    # HTTP 500 json kaynağı
    entry3 = {"slug": "j", "name": "J", "city": "Bursa", "kind": "json", "list_url": "https://j.test/api", "pagination": "none", "json_keys": {"items": "", "name": "name"}}
    respx_mock.get("https://j.test/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get("https://j.test/api").mock(return_value=httpx.Response(500))
    assert col._collect_entry(entry3, httpx.Client(), tp.Throttle(0)) == []


@pytest.mark.respx(assert_all_called=False)
def test_collect_cursor_pagination_stops_when_page_ignored(respx_mock):
    """Sayfalama parametresi yok sayılırsa (hep aynı sayfa) döngü durur."""
    entry = {
        "slug": "cursor", "name": "Cursor Park", "city": "İstanbul", "kind": "html",
        "list_url": "https://cursor.test/firmalar", "pagination": "page_param:per_page",
        "page_start": 72, "page_step": 36, "page_size": 2, "max_pages": 50,
        "selectors": {"item": "div.f", "name": "span"},
    }
    respx_mock.get("https://cursor.test/robots.txt").mock(return_value=httpx.Response(404))
    route = respx_mock.get(url__regex=r"https://cursor\.test/firmalar.*").mock(
        return_value=httpx.Response(200, text="<div class='f'><span>A</span></div><div class='f'><span>B</span></div>")
    )
    col = tp.TeknoparkCollector(client=httpx.Client(), delay=0)
    companies = col._collect_entry(entry, httpx.Client(), tp.Throttle(0))
    assert [c.name for c in companies] == ["A", "B"]
    assert route.call_count == 2
    assert "per_page=72" in str(route.calls[1].request.url)


@pytest.mark.respx(assert_all_called=False)
def test_collect_post_detail(respx_mock, entries):
    entry = entries["entertech-istanbul-teknokent"]
    respx_mock.get("https://entertech.com.tr/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.get(entry["list_url"]).mock(return_value=httpx.Response(200, content=_read_bytes("entertech-istanbul-teknokent.html")))
    post = respx_mock.post("https://entertech.com.tr/wp-admin/admin-ajax.php").mock(
        return_value=httpx.Response(200, content=_read_bytes("entertech-istanbul-teknokent-detail.html"))
    )
    col = _collector(slugs=["entertech-istanbul-teknokent"])
    out = list(col.collect())
    assert len(out) >= 6
    assert post.called
    body = post.calls[0].request.content.decode()
    assert "action=get_firm_details" in body and "post_id=80086" in body
    assert out[0].website == "http://www.dgpays.com"
    assert out[0].emails == ["info@dgpays.com"]


# ---------------------------------------------------------------------------
# Canlı ağ testi
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_live_aso_teknopark():
    col = tp.TeknoparkCollector(slugs=["aso-teknopark"])
    out = list(col.collect(cities=["Ankara"]))
    assert len(out) >= 30
    assert sum(1 for c in out if c.website) >= 20
    assert all(c.city == "Ankara" and c.is_teknopark for c in out)
