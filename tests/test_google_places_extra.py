"""Google Places toplayıcısı için ek (kırmaya yönelik) testler: bozuk yanıtlar, Türkçe karakterler,
önbellek/sayaç dosyası bozulmaları, tekrar eden şehirler, yeniden deneme ve bütçe davranışı."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
import pytest
import respx

from stajbot.collectors import google_places as gp
from stajbot.collectors.base import RawCompany
from stajbot.collectors.google_places import (
    API_URL,
    CITY_RECTANGLES,
    QUERY_TEMPLATES,
    GooglePlacesCollector,
    detect_city_district,
    estimate_cost,
    is_noise_name,
    parse_response,
    place_to_raw,
)
from stajbot.enums import TARGET_CITIES, normalize_city


def _place(pid, name, city="Konya", district="Selçuklu", **extra):
    p = {
        "id": pid,
        "displayName": {"text": name, "languageCode": "tr"},
        "formattedAddress": f"Mah. Sk. No:1, 42000 {district}/{city}, Türkiye",
        "addressComponents": [
            {"longText": district, "types": ["administrative_area_level_2", "political"]},
            {"longText": city, "types": ["administrative_area_level_1", "political"]},
        ],
        "googleMapsUri": f"https://maps.google.com/?cid={pid}",
        "types": ["corporate_office", "point_of_interest", "establishment"],
        "businessStatus": "OPERATIONAL",
    }
    p.update(extra)
    return p


def _collector(tmp_path, **kw):
    kw.setdefault("api_key", "k")
    kw.setdefault("client", httpx.Client())
    kw.setdefault("queries", ["q"])
    kw.setdefault("delay_sec", 0)
    kw.setdefault("data_dir", tmp_path)
    kw.setdefault("use_cache", False)
    return GooglePlacesCollector(**kw)


# --- sözleşme / statik kontroller ---------------------------------------------------

def test_module_contract_static():
    src = Path(gp.__file__).read_text(encoding="utf-8")
    assert "print(" not in src, "Modül print kullanmamalı (MIMARI.md 13)"
    col = GooglePlacesCollector(api_key="k", client=httpx.Client())
    assert col.name == "google_places"
    assert callable(col.collect)
    # her hedef şehir için dikdörtgen var
    for c in TARGET_CITIES:
        assert c in CITY_RECTANGLES, c
        for low_lat, low_lng, high_lat, high_lng in CITY_RECTANGLES[c]:
            assert low_lat < high_lat and low_lng < high_lng
    assert len(QUERY_TEMPLATES) >= 5


def test_constructor_clamps_and_defaults():
    col = GooglePlacesCollector(api_key="k", client=httpx.Client(), queries=[], max_pages_per_query=0, page_size=500)
    assert col.queries == QUERY_TEMPLATES
    assert col.max_pages_per_query == 1
    assert col.page_size == 20
    col = GooglePlacesCollector(api_key="k", client=httpx.Client(), max_pages_per_query=10)
    assert col.max_pages_per_query == 3


# --- bozuk yanıtlar -----------------------------------------------------------------

def test_parse_malformed_places_do_not_raise():
    data = {
        "places": [
            None,
            "metin",
            42,
            {"id": "a", "displayName": "Düz Metin Yazılım"},  # displayName dict değil
            {"id": "b", "displayName": {"text": "Bozuk Bileşen Teknoloji"}, "addressComponents": ["x", None, {"types": "political"}]},
            {"id": "c", "displayName": {"text": "Tip Metni Bilişim"}, "types": "corporate_office", "primaryType": 5, "rating": "4.5"},
            {"id": "d", "displayName": {"text": "   "}},
            {"id": "e", "displayName": {"text": None}},
            {"id": 123, "displayName": {"text": "Sayısal Kimlik Yazılım"}, "websiteUri": 77, "googleMapsUri": ""},
        ]
    }
    out = parse_response(data, "Konya")
    names = [r.name for r in out]
    assert names == ["Düz Metin Yazılım", "Bozuk Bileşen Teknoloji", "Tip Metni Bilişim", "Sayısal Kimlik Yazılım"]
    c = out[2]
    assert c.raw["types"] == ["corporate_office"]
    assert c.tags == [] and c.raw["primary_type"] is None and c.raw["rating"] is None
    d = out[3]
    assert d.raw["place_id"] is None and d.website is None and d.source_url is None
    assert all(r.city == "Konya" for r in out)


def test_parse_response_non_list_places_and_wrong_types():
    assert parse_response({"places": {"a": 1}}, "Konya") == []
    assert parse_response({"places": "abc"}, "Konya") == []
    assert parse_response({"places": None}, "Konya") == []
    assert parse_response([], "Konya") == []
    assert parse_response(None, "Konya") == []
    assert parse_response("{}", "Konya") == []
    assert place_to_raw("x", "Konya") is None
    assert place_to_raw({"businessStatus": 5, "displayName": {"text": "Statü Sayı Yazılım"}}, "Konya") is not None


def test_place_to_raw_truncates_long_fields():
    long_name = "Çok Uzun Yazılım " * 40
    rc = place_to_raw({"id": "a", "displayName": {"text": long_name}}, "Konya")
    assert rc is not None and len(rc.name) <= 255
    rc = place_to_raw({"id": "a", "displayName": {"text": "A Yazılım"}, "websiteUri": "https://firma.com/" + "x" * 1000}, "Konya")
    assert len(rc.website) <= 512


# --- şehir / ilçe tespiti ------------------------------------------------------------

def test_detect_city_ignores_numeric_slash_patterns():
    # "No:3/5" il/ilçe değildir -> sorgulanan şehre düşer
    assert detect_city_district({"formattedAddress": "Mah. Sk. No:3/5, Türkiye"}, "Konya") == ("Konya", None)
    # hem sayı kalıbı hem gerçek İlçe/İl: gerçek olan seçilir
    assert detect_city_district({"formattedAddress": "A Blok No:3/5, 16110 Nilüfer/Bursa, Türkiye"}, "Konya") == ("Bursa", "Nilüfer")


def test_detect_city_postal_code_not_confused_with_street_number():
    # "35100. Sk." bir sokak numarasıdır, İzmir posta kodu değil
    assert detect_city_district({"formattedAddress": "Fevzi Çakmak Mah. 35100. Sk. No:5, Türkiye"}, "Konya") == ("Konya", None)
    # gerçek posta kodu
    assert detect_city_district({"formattedAddress": "Fevzi Çakmak Mah. 10562. Sk. No:5, 42050 Türkiye"}, None) == ("Konya", None)
    assert detect_city_district({"formattedAddress": "Yol, 06800 Türkiye"}, None) == ("Ankara", None)


def test_detect_city_unicode_and_locality_handling():
    place = {
        "addressComponents": [
            {"longText": "İstanbul", "types": ["locality", "political"]},
            {"longText": "Kadıköy", "types": ["administrative_area_level_2", "political"]},
            {"longText": "İstanbul", "types": ["administrative_area_level_1", "political"]},
        ]
    }
    assert detect_city_district(place, "Konya") == ("İstanbul", "Kadıköy")
    # ilçe yok, locality = il -> ilçe None
    place = {
        "addressComponents": [
            {"longText": "İzmir", "types": ["locality", "political"]},
            {"longText": "İzmir", "types": ["administrative_area_level_1", "political"]},
        ]
    }
    assert detect_city_district(place, None) == ("İzmir", None)
    # formattedAddress metin değil
    assert detect_city_district({"formattedAddress": 123, "addressComponents": "x"}, "Mersin") == ("Mersin", None)
    # Türkçe karakterli İlçe/İl kalıbı normalize_city ile eşleşir
    city, district = detect_city_district({"formattedAddress": "Zafer Mah. 26010 Odunpazarı/Eskişehir, Türkiye"}, None)
    assert normalize_city(city) == "Eskişehir" and district == "Odunpazarı"


# --- web sitesi ----------------------------------------------------------------------

def test_website_keeps_legitimate_params_and_strips_tracking():
    def site(url):
        return place_to_raw({"id": "a", "displayName": {"text": "T Yazılım"}, "websiteUri": url}, "Konya").website

    assert site("https://firma.com/?reference=abc&refid=3&ref=gmb") == "https://firma.com/?reference=abc&refid=3"
    assert site("https://Firma.COM/sayfa#bolum?utm_campaign=x") == "https://firma.com/sayfa"
    assert site("https://firma.com/?utm_source=a&gclid=b&fbclid=c") == "https://firma.com/"
    assert site("firma.com.tr") == "https://firma.com.tr"
    assert site("   ") is None
    assert site("https://sites.google.com/view/firma") is None
    assert site("https://www.instagram.com/firma/") is None
    assert site("https://x.com/firma") is None
    # "x.com" ile biten başka alan adları sosyal sayılmaz
    assert site("https://matrix.com/") == "https://matrix.com/"


def test_linkedin_subdomain_moved_to_linkedin_url():
    rc = place_to_raw({"id": "a", "displayName": {"text": "T Yazılım"}, "websiteUri": "https://tr.linkedin.com/company/firma"}, "Konya")
    assert rc.website is None and rc.linkedin_url == "https://tr.linkedin.com/company/firma"


# --- gürültü ve teknopark --------------------------------------------------------------

def test_noise_turkish_case_variants():
    assert is_noise_name("ÖZEL BİLİŞİM KURSU")
    assert is_noise_name("İnternet Kafe Net")
    assert is_noise_name("Sürücü Kursu Yıldız")
    assert not is_noise_name("Kurşad Yazılım")
    assert not is_noise_name("Kursat Teknoloji")
    assert not is_noise_name("Telefonika Yazılım")
    assert is_noise_name("")
    assert is_noise_name(None)


def test_teknopark_name_extracted_from_address():
    rc = place_to_raw(
        {"id": "a", "displayName": {"text": "T Yazılım"}, "formattedAddress": "ODTÜ Teknokent, Üniversiteler Mah. 06800 Çankaya/Ankara, Türkiye"},
        None,
    )
    assert rc.is_teknopark is True
    assert rc.teknopark_name == "ODTÜ Teknokent"
    assert rc.city == "Ankara" and rc.district == "Çankaya"
    rc = place_to_raw({"id": "b", "displayName": {"text": "T Yazılım"}, "formattedAddress": "Bilkent Cyberpark, Ankara"}, None)
    assert rc.is_teknopark and rc.teknopark_name == "Bilkent Cyberpark"
    rc = place_to_raw({"id": "c", "displayName": {"text": "T Yazılım"}, "formattedAddress": "Sıradan Cad. No:1, Konya"}, None)
    assert rc.is_teknopark is False and rc.teknopark_name is None


# --- collect(): dosya sistemi ve ağ kenar durumları -------------------------------------

def test_corrupt_counter_and_cache_files_do_not_raise(tmp_path):
    (tmp_path / "request_counter.json").write_text("[1, 2]", encoding="utf-8")
    cache_dir = tmp_path / "konya"
    cache_dir.mkdir()
    (cache_dir / "q_r0_p1.json").write_text("{bozuk json", encoding="utf-8")
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": [_place("P1", "Bir Yazılım")]}))
        col = _collector(tmp_path, use_cache=True)
        out = list(col.collect(cities=["Konya"]))
    assert [r.name for r in out] == ["Bir Yazılım"]
    assert route.call_count == 1  # bozuk önbellek yok sayıldı
    counter = json.loads((tmp_path / "request_counter.json").read_text(encoding="utf-8"))
    assert isinstance(counter, dict) and sum(counter.values()) == 1
    # bozuk önbellek dosyası geçerli yanıtla üzerine yazıldı
    assert json.loads((cache_dir / "q_r0_p1.json").read_text(encoding="utf-8"))["places"][0]["id"] == "P1"


def test_counter_preserves_other_months_and_warns_on_free_cap(tmp_path, caplog):
    import datetime as dt

    key = dt.datetime.now().strftime("%Y-%m")
    (tmp_path / "request_counter.json").write_text(json.dumps({"2000-01": 7, key: 1000}), encoding="utf-8")
    with respx.mock() as rsps:
        rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": []}))
        col = _collector(tmp_path)
        with caplog.at_level(logging.WARNING, logger="stajbot"):
            list(col.collect(cities=["Konya"]))
    counter = json.loads((tmp_path / "request_counter.json").read_text(encoding="utf-8"))
    assert counter["2000-01"] == 7 and counter[key] == 1001
    assert col.monthly_requests == 1001
    assert any("ücretsiz" in r.getMessage() for r in caplog.records)


def test_cache_list_body_and_expired_cache_are_ignored(tmp_path):
    cache_dir = tmp_path / "konya"
    cache_dir.mkdir()
    p1 = cache_dir / "q_r0_p1.json"
    p1.write_text("[1, 2]", encoding="utf-8")  # geçerli JSON ama dict değil
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": []}))
        col = _collector(tmp_path, use_cache=True, cache_ttl_days=7)
        list(col.collect(cities=["Konya"]))
        assert route.call_count == 1
        # şimdi önbellek geçerli; eskitelim -> yeniden API'ye gitmeli
        old = time.time() - 10 * 86400
        os.utime(p1, (old, old))
        col2 = _collector(tmp_path, use_cache=True, cache_ttl_days=7)
        list(col2.collect(cities=["Konya"]))
        assert route.call_count == 2 and col2.cache_hits == 0


def test_200_non_dict_or_html_body_does_not_raise_or_cache(tmp_path, caplog):
    with respx.mock() as rsps:
        rsps.post(API_URL).mock(
            side_effect=[
                httpx.Response(200, json=[1, 2]),
                httpx.Response(200, text="<html>Cloudflare</html>", headers={"content-type": "text/html"}),
            ]
        )
        col = _collector(tmp_path, queries=["a", "b"], use_cache=True)
        with caplog.at_level(logging.WARNING, logger="stajbot"):
            out = list(col.collect(cities=["Konya"]))
    assert out == []
    assert not list((tmp_path / "konya").glob("*.json")) if (tmp_path / "konya").exists() else True
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


def test_error_bodies_in_odd_shapes_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(gp.time, "sleep", lambda *_: None)
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(
            side_effect=[
                httpx.Response(400, json=[1, 2]),
                httpx.Response(400, text="düz metin hata"),
                httpx.Response(400, json={"error": "metin"}),
                httpx.Response(500, text="<html>502</html>"),
                httpx.Response(200, json={"places": [_place("P9", "Son Yazılım")]}),
            ]
        )
        col = _collector(tmp_path, queries=["a", "b", "c", "d"])
        out = list(col.collect(cities=["Konya"]))
    assert [r.raw["place_id"] for r in out] == ["P9"]
    assert route.call_count == 5


def test_401_aborts_whole_run_including_other_cities(tmp_path, caplog):
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED", "message": "bad key"}}))
        col = _collector(tmp_path, queries=["a", "b"])
        with caplog.at_level(logging.ERROR, logger="stajbot"):
            out = list(col.collect(cities=["Konya", "Ankara", "İzmir"]))
    assert out == [] and route.call_count == 1
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert msgs and "anahtar" in msgs[0].lower()


def test_duplicate_city_spellings_scanned_once(tmp_path):
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": [_place("P1", "Bir Yazılım")]}))
        col = _collector(tmp_path)
        out = list(col.collect(cities=["Konya", "konya", "KONYA", "  ", "", None]))
    assert route.call_count == 1
    assert len(out) == 1


def test_cities_none_uses_settings_target_cities(tmp_path, monkeypatch):
    from stajbot import config

    s = config.get_settings()
    monkeypatch.setattr(s, "target_cities", ["Konya", "Afyonkarahisar"])
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": []}))
        col = _collector(tmp_path)
        list(col.collect())
    assert route.call_count == 2
    rects = [json.loads(c.request.content)["locationRestriction"]["rectangle"]["low"]["latitude"] for c in route.calls]
    assert rects == [CITY_RECTANGLES["Konya"][0][0], CITY_RECTANGLES["Afyonkarahisar"][0][0]]


def test_cache_paths_are_windows_safe_for_unicode_queries(tmp_path):
    query = "ar-ge merkezi / yazılım: şirketi? *çözüm* <İstanbul>"
    with respx.mock() as rsps:
        rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": []}))
        col = _collector(tmp_path, queries=[query], use_cache=True)
        list(col.collect(cities=["İstanbul"]))
    files = sorted(p.name for p in (tmp_path / "istanbul").glob("*.json"))
    assert len(files) == 2  # İstanbul: iki dikdörtgen (r0, r1), tek sayfa
    for name in files:
        assert re.fullmatch(r"[a-z0-9\-_]+\.json", name), name
        assert not re.search(r'[<>:"/\\|?*]', name)


def test_run_twice_with_cache_is_deterministic_and_free(tmp_path):
    page1 = {"places": [_place("P1", "Bir Yazılım"), _place("P2", "İki Bilişim")], "nextPageToken": "T2"}
    page2 = {"places": [_place("P3", "Üç Teknoloji")]}
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)])
        first = list(_collector(tmp_path, use_cache=True).collect(cities=["Konya"]))
        second_col = _collector(tmp_path, use_cache=True)
        second = list(second_col.collect(cities=["Konya"]))
    assert route.call_count == 2
    assert [r.raw["place_id"] for r in first] == ["P1", "P2", "P3"]
    assert [r.raw["place_id"] for r in second] == ["P1", "P2", "P3"]
    assert second_col.requests_made == 0 and second_col.cache_hits == 2
    # RawCompany alanları aynı (idempotent kaynak)
    assert [(r.name, r.city, r.district, r.website) for r in first] == [(r.name, r.city, r.district, r.website) for r in second]


def test_budget_exhausted_mid_run_stops_but_keeps_results(tmp_path, caplog):
    page = {"places": [_place("P1", "Bir Yazılım")]}
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json=page))
        col = _collector(tmp_path, queries=["a", "b", "c"], max_requests=1)
        with caplog.at_level(logging.WARNING, logger="stajbot"):
            out = list(col.collect(cities=["Konya", "Ankara"]))
    assert route.call_count == 1 and len(out) == 1
    assert any("bütçe" in r.getMessage() for r in caplog.records)


def test_max_requests_zero_makes_no_calls(tmp_path):
    with respx.mock(assert_all_called=False) as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": [_place("P1", "X")]}))
        col = _collector(tmp_path, max_requests=0)
        assert list(col.collect(cities=["Konya"])) == []
    assert route.call_count == 0


def test_network_error_then_success_retries(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(gp.time, "sleep", lambda s: sleeps.append(s))
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(
            side_effect=[httpx.ReadTimeout("t"), httpx.Response(503, text="x"), httpx.Response(200, json={"places": [_place("P1", "X Yazılım")]})]
        )
        col = _collector(tmp_path, max_retries=3)
        out = list(col.collect(cities=["Konya"]))
    assert [r.raw["place_id"] for r in out] == ["P1"]
    assert route.call_count == 3
    assert sleeps == [1.0, 2.0]  # üstel bekleme
    assert col.requests_made == 2  # ağ hatası yanıt sayılmaz, 503 + 200 sayılır


def test_dedupe_without_place_id_uses_folded_name(tmp_path):
    page = {
        "places": [
            {"displayName": {"text": "ÖRNEK YAZILIM"}},
            {"displayName": {"text": "örnek yazılım"}},
            {"displayName": {"text": "Ornek Yazilim"}},
        ]
    }
    with respx.mock() as rsps:
        rsps.post(API_URL).mock(return_value=httpx.Response(200, json=page))
        out = list(_collector(tmp_path).collect(cities=["Konya"]))
    assert len(out) == 1


def test_estimate_cost_edge_cases():
    assert estimate_cost(-5) == 0.0
    assert estimate_cost(1, free_events_used=-10) == 0.0
    assert estimate_cost(1001) == pytest.approx(0.035)
    assert estimate_cost(1000, free_events_used=999) == pytest.approx(34.965)
    assert estimate_cost(5, tier="enterprise_atmosphere", free_events_used=1000) == pytest.approx(0.2)


def test_rawcompany_fields_match_contract_types():
    rc = place_to_raw(_place("P1", "Bir Yazılım", websiteUri="https://bir.com", internationalPhoneNumber="+90 332 111 11 11"), "Konya", "q")
    assert isinstance(rc, RawCompany)
    assert rc.source == "google_places"
    assert isinstance(rc.tags, list) and isinstance(rc.emails, list) and isinstance(rc.raw, dict)
    assert rc.raw["query"] == "q" and rc.raw["queried_city"] == "Konya" and rc.raw["detected_city"] == "Konya"
    # JSON'a yazılabilir olmalı (CompanySource.raw JSON sütunu)
    json.dumps(rc.raw, ensure_ascii=False)
