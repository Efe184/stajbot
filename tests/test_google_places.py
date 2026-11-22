"""Google Places toplayıcısı testleri (çevrimdışı; respx ile sahte API)."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from stajbot.collectors import google_places as gp
from stajbot.collectors.base import RawCompany
from stajbot.collectors.google_places import (
    API_URL,
    FIELD_MASK,
    GooglePlacesCollector,
    detect_city_district,
    estimate_cost,
    is_noise_name,
    parse_response,
)


@pytest.fixture()
def gp_fixtures(fixtures_dir):
    return fixtures_dir / "google_places"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- parse_response ---------------------------------------------------------------

def test_parse_response_fixture(gp_fixtures):
    data = _load(gp_fixtures / "sample_response.json")
    out = parse_response(data, "Konya")
    # İkinci kayıt CLOSED_TEMPORARILY -> atlanır
    assert len(out) == 1
    rc = out[0]
    assert isinstance(rc, RawCompany)
    assert rc.source == "google_places"
    assert rc.name == "Örnek Yazılım ve Bilişim Teknolojileri A.Ş."
    assert rc.website == "https://www.ornekyazilim.com.tr/"
    assert rc.phone == "+90 332 000 00 00"
    assert rc.city == "Konya"
    assert rc.district == "Selçuklu"
    assert rc.address.startswith("Konya Teknokent")
    assert rc.is_teknopark is True
    assert rc.source_url == "https://maps.google.com/?cid=1234567890123456789"
    assert rc.raw["place_id"] == "ChIJ0000000000000000000000A"
    assert rc.raw["types"] == ["corporate_office", "consultant", "point_of_interest", "establishment"]
    assert rc.raw["rating"] == 4.6
    assert rc.raw["userRatingCount"] == 27
    assert rc.raw["googleMapsUri"] == rc.source_url
    assert "corporate_office" in rc.tags


def test_parse_response_docs_fields_example(gp_fixtures):
    data = _load(gp_fixtures / "docs_example_response_fields.json")
    out = parse_response(data, "İzmir")
    assert len(out) == 2
    assert out[0].name == "Mother Chu's Vegetarian Kitchen"
    assert out[0].website == "http://www.motherchusvegetarian.com.au/"
    # addressComponents yok, adres kalıbı Türkçe değil -> sorgulanan şehre düşer
    assert out[0].city == "İzmir"
    assert out[0].raw["place_id"] is None


def test_parse_response_future_opening_and_pagination(gp_fixtures):
    fut = _load(gp_fixtures / "docs_example_response_future_opening.json")
    out = parse_response(fut, "Ankara")
    assert len(out) == 1 and out[0].raw["business_status"] == "FUTURE_OPENING"
    # yalnızca id içeren sayfalar: displayName yok -> kayıt üretilmez ama hata da yok
    pag = _load(gp_fixtures / "docs_example_response_pagination.json")
    assert parse_response(pag, "Ankara") == []


def test_parse_response_error_and_empty(gp_fixtures):
    err = _load(gp_fixtures / "sample_error_response.json")
    assert parse_response(err, "Konya") == []
    assert parse_response({}, "Konya") == []
    assert parse_response({"places": []}, "Konya") == []


def test_closed_permanently_and_noise_skipped():
    data = {
        "places": [
            {"id": "a", "displayName": {"text": "Kapalı Yazılım"}, "businessStatus": "CLOSED_PERMANENTLY"},
            {"id": "b", "displayName": {"text": "Mega Bilgisayar Tamir Servisi"}, "businessStatus": "OPERATIONAL"},
            {"id": "c", "displayName": {"text": "Atlas Yazılım"}, "businessStatus": "OPERATIONAL"},
            {"id": "d", "displayName": {"text": "Statüsüz Teknoloji"}},
        ]
    }
    out = parse_response(data, "Bursa")
    assert [r.raw["place_id"] for r in out] == ["c", "d"]
    assert is_noise_name("Özel Bilişim Kursu")
    assert not is_noise_name("Telefonika Yazılım")


def test_website_normalisation():
    def mk(url):
        return {"id": "x", "displayName": {"text": "Test Yazılım"}, "websiteUri": url}

    rc = parse_response({"places": [mk("https://WWW.Firma.com/?utm_source=gmb&utm_medium=x&lang=tr")]}, "Konya")[0]
    assert rc.website == "https://www.firma.com/?lang=tr"
    rc = parse_response({"places": [mk("https://www.facebook.com/firma")]}, "Konya")[0]
    assert rc.website is None and rc.linkedin_url is None
    rc = parse_response({"places": [mk("https://www.linkedin.com/company/firma")]}, "Konya")[0]
    assert rc.website is None and rc.linkedin_url == "https://www.linkedin.com/company/firma"


# --- şehir tespiti ----------------------------------------------------------------

def test_detect_city_from_components():
    place = {
        "addressComponents": [
            {"longText": "Çankaya", "types": ["administrative_area_level_2", "political"]},
            {"longText": "Ankara", "types": ["administrative_area_level_1", "political"]},
        ],
        "formattedAddress": "Falan Mah. 06800 Çankaya/Ankara",
    }
    assert detect_city_district(place, "Konya") == ("Ankara", "Çankaya")


def test_detect_city_fallbacks():
    assert detect_city_district({"formattedAddress": "X Mah. Y Sk. No:3, 16110 Nilüfer/Bursa, Türkiye"}, None) == ("Bursa", "Nilüfer")
    assert detect_city_district({"formattedAddress": "Bir yer, 35100 Türkiye"}, None) == ("İzmir", None)
    assert detect_city_district({}, "Mersin") == ("Mersin", None)


# --- maliyet ----------------------------------------------------------------------

def test_estimate_cost():
    assert estimate_cost(0) == 0.0
    assert estimate_cost(240) == 0.0  # ücretsiz Enterprise kotası içinde
    assert estimate_cost(240, free_events_used=1000) == pytest.approx(8.40)
    assert estimate_cost(1240) == pytest.approx(8.40)
    assert estimate_cost(6000, tier="pro") == pytest.approx(32.0)
    assert estimate_cost(10_000, tier="essentials") == 0.0
    with pytest.raises(ValueError):
        estimate_cost(1, tier="yok")


# --- collect() --------------------------------------------------------------------

def _place(pid, name, city="Konya", district="Selçuklu", status="OPERATIONAL", website=None):
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
    }
    if status:
        p["businessStatus"] = status
    if website:
        p["websiteUri"] = website
    return p


def test_collect_two_pages_dedupe_and_city(tmp_path):
    page1 = {
        "places": [
            _place("P1", "Birinci Yazılım", website="https://birinci.com"),
            _place("P2", "İkinci Bilişim"),
            _place("P3", "Kapalı Firma", status="CLOSED_PERMANENTLY"),
        ],
        "nextPageToken": "TOKEN-2",
    }
    page2 = {
        "places": [
            _place("P2", "İkinci Bilişim"),  # tekrar -> tekilleştirilir
            _place("P4", "Dördüncü Teknoloji", city="Ankara", district="Çankaya"),  # sınır dışı sonuç
        ]
    }
    with respx.mock(assert_all_called=True) as rsps:
        route = rsps.post(API_URL).mock(side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)])
        col = GooglePlacesCollector(
            api_key="TEST-KEY",
            client=httpx.Client(),
            queries=["yazılım şirketi"],
            max_pages_per_query=3,
            delay_sec=0,
            data_dir=tmp_path,
            use_cache=False,
        )
        out = list(col.collect(cities=["Konya"]))

    assert [r.raw["place_id"] for r in out] == ["P1", "P2", "P4"]
    assert [r.city for r in out] == ["Konya", "Konya", "Ankara"]
    assert out[0].website == "https://birinci.com"
    assert out[2].district == "Çankaya"
    assert all(r.raw["queried_city"] == "Konya" for r in out)
    assert col.requests_made == 2
    assert route.call_count == 2

    req1 = route.calls[0].request
    body1 = json.loads(req1.content)
    assert req1.headers["X-Goog-Api-Key"] == "TEST-KEY"
    assert req1.headers["X-Goog-FieldMask"] == FIELD_MASK
    assert " " not in FIELD_MASK and "places.websiteUri" in FIELD_MASK and "nextPageToken" in FIELD_MASK
    assert body1["textQuery"] == "yazılım şirketi"
    assert body1["languageCode"] == "tr" and body1["regionCode"] == "TR"
    assert body1["locationRestriction"]["rectangle"]["low"] == {"latitude": 37.70, "longitude": 32.20}
    assert "pageToken" not in body1

    body2 = json.loads(route.calls[1].request.content)
    assert body2["pageToken"] == "TOKEN-2"
    # sayfa 2'de diğer tüm alanlar aynı kalmalı
    assert {k: v for k, v in body2.items() if k != "pageToken"} == body1

    # aylık sayaç yazıldı
    counter = json.loads((tmp_path / "request_counter.json").read_text(encoding="utf-8"))
    assert sum(counter.values()) == 2


def test_collect_dedupes_across_queries_and_uses_cache(tmp_path):
    page = {"places": [_place("P1", "Birinci Yazılım")]}
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json=page))
        col = GooglePlacesCollector(
            api_key="k", client=httpx.Client(), queries=["a", "b"], delay_sec=0, data_dir=tmp_path, use_cache=True
        )
        out = list(col.collect(cities=["Konya"]))
        assert len(out) == 1
        assert route.call_count == 2  # iki sorgu, tek sayfa
        # ikinci çalıştırma önbellekten okur, API'ye gitmez
        col2 = GooglePlacesCollector(api_key="k", client=httpx.Client(), queries=["a", "b"], delay_sec=0, data_dir=tmp_path)
        out2 = list(col2.collect(cities=["Konya"]))
        assert len(out2) == 1
        assert route.call_count == 2
        assert col2.requests_made == 0 and col2.cache_hits == 2
    assert (tmp_path / "konya" / "a_r0_p1.json").exists()


def test_collect_istanbul_two_rectangles_and_unknown_city(tmp_path):
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": []}))
        col = GooglePlacesCollector(api_key="k", client=httpx.Client(), queries=["q"], delay_sec=0, data_dir=tmp_path, use_cache=False)
        assert list(col.collect(cities=["istanbul", "Kocaeli"])) == []
        assert route.call_count == 3
        bodies = [json.loads(c.request.content) for c in route.calls]
        assert "locationRestriction" in bodies[0] and "locationRestriction" in bodies[1]
        assert bodies[0]["locationRestriction"] != bodies[1]["locationRestriction"]
        assert bodies[2]["textQuery"] == "q Kocaeli" and "locationRestriction" not in bodies[2]


def test_collect_missing_key_logs_warning_and_yields_nothing(caplog, tmp_path):
    with respx.mock(assert_all_called=False) as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json={"places": [_place("P1", "X Yazılım")]}))
        col = GooglePlacesCollector(api_key="", client=httpx.Client(), delay_sec=0, data_dir=tmp_path, use_cache=False)
        with caplog.at_level(logging.WARNING, logger="stajbot"):
            out = list(col.collect(cities=["Konya"]))
    assert out == []
    assert route.call_count == 0
    assert any("GOOGLE_PLACES_API_KEY" in r.getMessage() for r in caplog.records)


def test_collect_http_errors_do_not_raise(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gp.time, "sleep", lambda *_: None)
    page = {"places": [_place("P1", "Birinci Yazılım")]}
    with respx.mock() as rsps:
        # 1. sorgu: 429 sonra başarı; 2. sorgu: 400 hata zarfı; 3. sorgu: ağ hatası
        route = rsps.post(API_URL).mock(
            side_effect=[
                httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}),
                httpx.Response(200, json=page),
                httpx.Response(400, json={"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad"}}),
                httpx.ConnectError("boom"),
                httpx.ConnectError("boom"),
                httpx.ConnectError("boom"),
                httpx.ConnectError("boom"),
            ]
        )
        col = GooglePlacesCollector(
            api_key="k", client=httpx.Client(), queries=["a", "b", "c"], delay_sec=0, data_dir=tmp_path, use_cache=False, max_retries=3
        )
        with caplog.at_level(logging.WARNING, logger="stajbot"):
            out = list(col.collect(cities=["Konya"]))
    assert [r.raw["place_id"] for r in out] == ["P1"]
    assert route.call_count == 7
    assert any("HTTP 400" in r.getMessage() for r in caplog.records)


def test_collect_aborts_on_permission_denied(tmp_path, caplog):
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(
            return_value=httpx.Response(403, json={"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "API not enabled"}})
        )
        col = GooglePlacesCollector(api_key="k", client=httpx.Client(), queries=["a", "b"], delay_sec=0, data_dir=tmp_path, use_cache=False)
        with caplog.at_level(logging.ERROR, logger="stajbot"):
            out = list(col.collect(cities=["Konya", "Ankara"]))
    assert out == []
    assert route.call_count == 1  # ilk 403'te durur
    assert any("Places API (New)" in r.getMessage() for r in caplog.records)


def test_collect_request_budget(tmp_path):
    page = {"places": [_place("P1", "Birinci Yazılım")], "nextPageToken": "T"}
    with respx.mock() as rsps:
        route = rsps.post(API_URL).mock(return_value=httpx.Response(200, json=page))
        col = GooglePlacesCollector(
            api_key="k", client=httpx.Client(), queries=["a", "b"], delay_sec=0, data_dir=tmp_path, use_cache=False, max_requests=2
        )
        out = list(col.collect(cities=["Konya"]))
    assert len(out) == 1
    assert route.call_count == 2
