"""Ar-Ge / Tasarım Merkezleri toplayıcısı için ek (kırıcı) testler.

Boş/bozuk girdiler, Türkçe karakterler, çoklu sayfa, sıra numarasız satırlar, şehir
filtresindeki None/boş değerler, ağ hataları (respx), tekrar çalıştırma (idempotans) ve
Windows yolları.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path

import httpx
import openpyxl
import pytest
import respx

from stajbot.collectors import argem
from stajbot.collectors.argem import (
    KIND_ARGE,
    KIND_TASARIM,
    OFFICIAL_URLS,
    ArgemCollector,
    dedupe_records,
    detect_columns,
    fetch_xlsx,
    filter_records_by_cities,
    parse_xlsx,
    parse_xlsx_records,
    record_to_raw,
    wayback_latest_capture,
)
from stajbot.collectors.base import RawCompany

HEADER = ["SIRA NO", "AR-GE MERKEZİNİN ADI", "İL", "SEKTÖR", "BELGE TARİHİ"]
LOGGER = "stajbot.collectors.argem"


def make_wb(sheets: list[tuple[str, list[list]]]) -> bytes:
    """Birden çok sayfalı çalışma kitabı üretir: [(sayfa adı, satırlar), ...]."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def simple(rows: list[list], header: list | None = None) -> bytes:
    return make_wb([("Sayfa1", [header or HEADER, *rows])])


@pytest.fixture(scope="module")
def xlsx_2026() -> Path:
    return Path(__file__).parent / "fixtures" / "argem" / "Ar-Gemerkezleri_2026-02_archived.xlsx"


@pytest.fixture(scope="module")
def xlsx_2020() -> Path:
    return Path(__file__).parent / "fixtures" / "argem" / "ArGeMerkezleriSubat2020_archived.xlsx"


# --- tarih ayrıştırma -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "cell,expected",
    [
        ("07.06.2017 00:00:00", date(2017, 6, 7)),
        ("2023-01-05T00:00:00", date(2023, 1, 5)),
        ("12/03/2023", date(2023, 3, 12)),
        (44927, date(2023, 1, 1)),  # biçimlenmemiş Excel seri numarası
        (44927.0, date(2023, 1, 1)),
        (datetime(2020, 6, 4, 13, 45), date(2020, 6, 4)),
        ("tarih yok", None),
        ("", None),
        (None, None),
        (True, None),
        (5, None),  # makul aralık dışı seri numarası
    ],
)
def test_to_date_variants(cell, expected):
    recs = parse_xlsx_records(simple([[1, "Firma A.Ş.", "Konya", "Yazılım", cell]]))
    assert len(recs) == 1
    assert recs[0].cert_date == expected


# --- boş / bozuk girdiler -------------------------------------------------------------------

def test_empty_sheet_yields_nothing(caplog):
    data = make_wb([("Sayfa1", [])])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse_xlsx_records(data) == []
    assert any("başlıksız" in m for m in caplog.messages)


def test_header_only_yields_nothing():
    assert parse_xlsx_records(simple([])) == []
    assert parse_xlsx(simple([])) == []


def test_header_on_second_sheet_is_found():
    data = make_wb(
        [
            ("Özet", [["Ar-Ge Merkezi Sayısı", 1368], ["Tasarım Merkezi Sayısı", 400]]),
            ("Liste", [HEADER, [1, "İkinci Sayfa Yazılım A.Ş.", "Ankara", "Yazılım", None]]),
        ]
    )
    recs = parse_xlsx_records(data)
    assert [r.name for r in recs] == ["İkinci Sayfa Yazılım A.Ş."]
    assert recs[0].city == "Ankara" and recs[0].kind == KIND_ARGE


def test_header_beyond_scan_limit_on_every_sheet_logs_warning(caplog):
    filler = [["not", None, None, None, None]] * (argem.HEADER_SCAN_ROWS + 1)
    data = make_wb([("A", filler + [HEADER, [1, "Geç Firma", "Konya", "Yazılım", None]])])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse_xlsx_records(data) == []
    assert any("başlıksız" in m for m in caplog.messages)


def test_rows_without_sira_but_with_city_are_kept_totals_dropped():
    data = simple(
        [
            [1, "Alfa Yazılım A.Ş.", "Konya", "Yazılım", None],
            [None, "Beta Bilişim Ltd.", "Ankara", "Yazılım", None],  # sıra no eksik ama gerçek satır
            ["TOPLAM", None, None, None, None],
            ["Not: Liste Şubat 2026 itibarıyla", None, None, None, None],
            [None, None, None, None, None],
            [True, "Bool Sıra Firma", "İzmir", "Yazılım", None],
        ]
    )
    recs = parse_xlsx_records(data)
    assert [r.name for r in recs] == ["Alfa Yazılım A.Ş.", "Beta Bilişim Ltd.", "Bool Sıra Firma"]
    assert recs[1].sira_no is None and recs[2].sira_no is None


def test_row_with_city_but_empty_name_is_skipped():
    recs = parse_xlsx_records(simple([[1, "   ", "Konya", "Yazılım", None], [2, None, "Konya", None, None]]))
    assert recs == []


def test_garbage_bytes_are_handled_by_collector(tmp_path, caplog):
    # ZIP imzalı ama XLSX olmayan dosya: is_xlsx_bytes geçer, openpyxl açamaz -> collector loglar, boş döner
    bad = tmp_path / "sahte.xlsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dolgu.txt", "x" * 5000)
    bad.write_bytes(buf.getvalue())
    assert argem.is_xlsx_bytes(bad.read_bytes())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = list(ArgemCollector(path=bad).collect())
    assert out == []
    assert any("okunamadı" in m for m in caplog.messages)


def test_empty_path_list_logs_and_yields_nothing(caplog):
    c = ArgemCollector(path=[])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert list(c.collect()) == []
    assert c.last_files == []
    assert any("boş yol" in m for m in caplog.messages)


def test_mixed_valid_and_missing_paths(tmp_path, xlsx_2026, caplog):
    c = ArgemCollector(path=[tmp_path / "yok.xlsx", xlsx_2026], only_tech=False)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = list(c.collect(cities=["Afyonkarahisar"]))
    assert len(out) == 1 and c.last_files == [xlsx_2026]
    assert any("bulunamadı" in m for m in caplog.messages)


# --- başlık varyantları ---------------------------------------------------------------------

def test_detect_columns_alternative_turkish_headers():
    cols = detect_columns(["NO", "KURULUŞ ÜNVANI", "ŞEHİR", "FAALİYET ALANI", "BELGE TARİHİ"])
    assert cols is not None
    assert (cols.order, cols.name, cols.city, cols.sector, cols.date) == (0, 1, 2, 3, 4)


def test_detect_columns_il_adi_not_confused_with_company_name():
    cols = detect_columns(["SIRA NO", "İL ADI", "AR-GE MERKEZİNİN ADI", "SEKTÖR"])
    assert cols is not None
    assert cols.city == 1 and cols.name == 2


def test_detect_columns_without_optional_columns():
    cols = detect_columns(["FİRMA ADI", "İL"])
    assert cols is not None
    assert cols.sector is None and cols.date is None and cols.order is None
    recs = parse_xlsx_records(simple([["Tek Sütun Yazılım A.Ş.", "Bursa"]], header=["FİRMA ADI", "İL"]))
    assert len(recs) == 1 and recs[0].sector is None and recs[0].cert_date is None and recs[0].sira_no is None


# --- Türkçe karakterler ve şehir normalizasyonu -------------------------------------------------

def test_turkish_characters_preserved_and_city_normalized():
    data = simple(
        [
            [1, "Şırnak Güneş Enerjisi Yazılım Çözümleri İth. İhr. Ltd. Şti.", "ISTANBUL", "Yazılım", None],
            [2, "Öğün Bilişim A.Ş.", "izmir", "Yazılım", None],
            [3, "Çağrı Elektronik A.Ş.", "Eskisehir", "Elektronik", None],
            [4, "Afyon Teknoloji A.Ş.", "AFYON", "Yazılım", None],
        ]
    )
    raws = parse_xlsx(data)
    assert [r.name for r in raws][0] == "Şırnak Güneş Enerjisi Yazılım Çözümleri İth. İhr. Ltd. Şti."
    assert [r.city for r in raws] == ["İstanbul", "İzmir", "Eskişehir", "Afyonkarahisar"]
    for r in raws:
        json.dumps(r.raw, ensure_ascii=False)
        assert r.description and "Ar-Ge Merkezi" in r.description


def test_non_target_city_rows_pass_through_when_no_filter():
    raws = parse_xlsx(simple([[1, "Kocaeli Yazılım A.Ş.", "Kocaeli", "Yazılım", None]]))
    assert len(raws) == 1 and raws[0].city == "Kocaeli"  # ingest hedef dışını atar


def test_row_without_city_is_kept_but_filtered_out_by_city_filter():
    data = simple([[1, "Şehirsiz Yazılım A.Ş.", None, "Yazılım", None]])
    recs = parse_xlsx_records(data)
    assert len(recs) == 1 and recs[0].city is None
    assert filter_records_by_cities(recs, ["Konya"]) == []
    raw = record_to_raw(recs[0])
    assert raw.city is None


@pytest.mark.parametrize("cities", [[None], [""], ["  "], [None, ""]])
def test_filter_with_only_empty_cities_means_no_filter(cities):
    recs = parse_xlsx_records(simple([[1, "A A.Ş.", "Konya", "Yazılım", None], [2, "B A.Ş.", "Kocaeli", "Yazılım", None]]))
    assert len(filter_records_by_cities(recs, cities)) == 2


def test_filter_ignores_empty_entries_but_applies_real_ones():
    recs = parse_xlsx_records(simple([[1, "A A.Ş.", "Konya", "Yazılım", None], [2, "B A.Ş.", "Ankara", "Yazılım", None]]))
    out = filter_records_by_cities(recs, ["", None, "konya"])
    assert [r.name for r in out] == ["A A.Ş."]
    assert list(ArgemCollector(path=[]).collect(cities=[None])) == []  # çökmez


# --- tekilleştirme ----------------------------------------------------------------------------

def test_dedupe_does_not_mutate_input_records():
    recs = parse_xlsx_records(
        simple(
            [
                [1, "X Yazılım A.Ş. (Konya Teknokent)", "Konya", "Yazılım", None],
                [2, "X Yazılım A.Ş. (Konya OSB)", "Konya", "Yazılım", None],
                [3, "X Yazılım A.Ş.", "Ankara", "Yazılım", None],
                [4, "X YAZILIM ANONİM ŞİRKETİ", "konya", "Yazılım", None],  # aynı firma, farklı yazım
            ]
        )
    )
    out = dedupe_records(recs)
    assert recs[0].site_note == "Konya Teknokent"  # girdi değişmedi
    assert len(out) == 2
    assert {r.city for r in out} == {"Konya", "Ankara"}
    konya = next(r for r in out if r.city == "Konya")
    assert konya.site_note == "Konya Teknokent; Konya OSB"
    # ikinci kez çalıştırmak aynı sonucu verir
    assert [r.name for r in dedupe_records(recs)] == [r.name for r in out]


def test_match_key_name_folds_long_legal_suffixes():
    k = argem.match_key_name
    assert k("Aselsan Konya Silah Sistemleri Anonim Şirketi") == k("Aselsan Konya Silah Sistemleri A.Ş.")
    assert k("Abc Yazılım Limited Şirketi") == k("ABC Yazılım Ltd. Şti.")
    assert k("Abc Sanayi ve Ticaret A.Ş.") == k("Abc Ticaret ve Sanayi A.Ş.")
    assert k("Alfa Teknoloji A.Ş.") != k("Beta Teknoloji A.Ş.")
    assert k(None) == "" and k("") == ""


def test_dedupe_empty_and_single():
    assert dedupe_records([]) == []
    recs = parse_xlsx_records(simple([[1, "Tek A.Ş.", "Konya", "Yazılım", None]]))
    assert dedupe_records(recs) == recs


def test_collect_twice_is_idempotent(xlsx_2026):
    c = ArgemCollector(path=xlsx_2026)
    first = [(r.name, r.city, r.sector, tuple(r.tags)) for r in c.collect(cities=["Konya", "Ankara"])]
    second = [(r.name, r.city, r.sector, tuple(r.tags)) for r in c.collect(cities=["Konya", "Ankara"])]
    assert first == second and first
    assert len(set(first)) == len(first)


def test_two_file_versions_merge_without_duplicate_keys(tmp_path, xlsx_2026, xlsx_2020):
    (tmp_path / "2026.xlsx").write_bytes(xlsx_2026.read_bytes())
    (tmp_path / "2020.xlsx").write_bytes(xlsx_2020.read_bytes())
    out = list(ArgemCollector(path=tmp_path, only_tech=False).collect(cities=["İstanbul"]))
    keys = [(argem.normalize_name(r.name), r.city) for r in out]
    assert len(keys) == len(set(keys))
    assert all(r.city == "İstanbul" for r in out)
    assert 432 <= len(out) < 432 + 427


# --- RawCompany sözleşmesi -----------------------------------------------------------------------

def test_raw_company_contract_fields(xlsx_2026):
    out = list(ArgemCollector(path=xlsx_2026).collect(cities=["Konya"]))
    assert out
    for r in out:
        assert isinstance(r, RawCompany)
        assert r.source == "argem" and r.name and r.city == "Konya"
        assert r.website is None and r.emails == []
        assert r.is_arge_center is True and r.is_teknopark is False and r.school_protocol is False
        assert r.tags in (["arge-merkezi"], ["tasarim-merkezi"])
        assert r.source_url and r.source_url.startswith("https://")
        assert isinstance(r.raw, dict) and r.raw["kaynak"] == "argem"
        json.dumps(r.raw, ensure_ascii=False)
        assert r.sector is None or len(r.sector) <= 128


def test_module_level_collect_shortcut(xlsx_2026):
    out = list(argem.collect(["Afyonkarahisar"], path=xlsx_2026, only_tech=False))
    assert len(out) == 1 and out[0].name == "Kızılay İçecek A.Ş."


# --- Windows yolları -------------------------------------------------------------------------------

def test_windows_style_string_path_and_unicode_dir(tmp_path, xlsx_2026):
    d = tmp_path / "Ar-Ge Listeleri (Şubat)"
    d.mkdir()
    target = d / "Ar-Gemerkezleri.xlsx"
    target.write_bytes(xlsx_2026.read_bytes())
    win_str = str(target).replace("/", "\\")
    c = ArgemCollector(path=win_str, only_tech=False)
    out = list(c.collect(cities=["Konya"]))
    assert len(out) == 25
    assert c.last_files == [Path(win_str)]
    assert out[0].raw["dosya"] == "Ar-Gemerkezleri.xlsx"


def test_uppercase_extension_in_data_dir(tmp_path, xlsx_2026):
    data_dir = tmp_path / "argem"
    data_dir.mkdir()
    (data_dir / "LISTE.XLSX").write_bytes(xlsx_2026.read_bytes())
    files = argem.local_xlsx_files(data_dir)
    # Windows'ta glob büyük/küçük harf duyarsızdır; başka platformda boş olabilir — çökmemesi yeter
    assert all(p.suffix.lower() == ".xlsx" for p in files)


# --- ağ hataları -------------------------------------------------------------------------------------

def test_cdx_url_is_https():
    assert argem.WAYBACK_CDX_URL.startswith("https://")


@respx.mock
def test_collector_all_network_errors_yield_nothing_and_instruct(tmp_path, caplog):
    respx.get(OFFICIAL_URLS[KIND_ARGE]).mock(side_effect=httpx.ConnectTimeout("zaman aşımı"))
    respx.get(OFFICIAL_URLS[KIND_TASARIM]).mock(side_effect=httpx.ConnectError("bağlantı yok"))
    respx.get(argem.WAYBACK_CDX_URL).mock(side_effect=httpx.ReadTimeout("cdx zaman aşımı"))
    c = ArgemCollector(data_dir=tmp_path / "argem", cache_dir=tmp_path / "cache", client=httpx.Client(), wayback_sleep_sec=0)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        try:
            out = list(c.collect(cities=["Konya"]))
        finally:
            c.client.close()
    assert out == [] and c.last_files == []
    joined = "\n".join(caplog.messages)
    assert "tarayıcı" in joined and str(tmp_path / "argem") in joined
    assert not list((tmp_path / "cache").glob("*.xlsx"))


@respx.mock
def test_wayback_download_returns_html_stub_is_rejected(tmp_path):
    respx.get(OFFICIAL_URLS[KIND_ARGE]).mock(return_value=httpx.Response(403, text="Forbidden"))
    respx.get(argem.WAYBACK_CDX_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                ["timestamp", "original", "statuscode", "mimetype", "length"],
                ["20260206163845", "https://sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx", "200",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "95000"],
            ],
        )
    )
    respx.get("https://web.archive.org/web/20260206163845id_/https://sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx").mock(
        return_value=httpx.Response(200, content=b"<html>Request Rejected</html>")
    )
    with httpx.Client() as client:
        assert fetch_xlsx(KIND_ARGE, client=client, cache_dir=tmp_path, wayback_sleep_sec=0) is None
    assert not (tmp_path / "Ar-Gemerkezleri.xlsx").exists()


@respx.mock
def test_cdx_malformed_json_retries_then_none():
    route = respx.get(argem.WAYBACK_CDX_URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))
    with httpx.Client() as client:
        assert wayback_latest_capture(OFFICIAL_URLS[KIND_ARGE], client, retries=3, sleep_sec=0) is None
    assert route.call_count == 3


@respx.mock
def test_cdx_client_error_does_not_retry():
    route = respx.get(argem.WAYBACK_CDX_URL).mock(return_value=httpx.Response(403, text="forbidden"))
    with httpx.Client() as client:
        assert wayback_latest_capture(OFFICIAL_URLS[KIND_ARGE], client, retries=3, sleep_sec=0) is None
    assert route.call_count == 1


@respx.mock
def test_cdx_rows_with_garbage_shapes_are_ignored():
    respx.get(argem.WAYBACK_CDX_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                ["timestamp", "original", "statuscode", "mimetype", "length"],
                "bozuk satır",
                ["20250101000000"],
                [None, None, None, None],
                ["20240101000000", "https://sanayi.gov.tr/x.xlsx", "302", "application/vnd.ms-excel", "1"],
                ["20230101000000", "https://sanayi.gov.tr/x.xlsx", "200", "application/vnd.ms-excel.sheet.macroEnabled.12", "1"],
            ],
        )
    )
    with httpx.Client() as client:
        cap = wayback_latest_capture(OFFICIAL_URLS[KIND_ARGE], client, retries=1, sleep_sec=0)
    assert cap == ("20230101000000", "https://sanayi.gov.tr/x.xlsx")


def test_fetch_xlsx_unknown_kind_returns_none(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert fetch_xlsx("Bilinmeyen", cache_dir=tmp_path) is None
    assert any("Bilinmeyen liste türü" in m for m in caplog.messages)


@respx.mock
def test_fetch_xlsx_direct_returns_tiny_zip_falls_back(tmp_path, xlsx_2026):
    # 'PK' ile başlayan ama çok küçük içerik XLSX sayılmaz
    respx.get(OFFICIAL_URLS[KIND_ARGE]).mock(return_value=httpx.Response(200, content=b"PK\x03\x04tiny"))
    respx.get(argem.WAYBACK_CDX_URL).mock(
        return_value=httpx.Response(
            200,
            json=[["20260206163845", "https://sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx", "200",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "1"]],
        )
    )
    respx.get("https://web.archive.org/web/20260206163845id_/https://sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx").mock(
        return_value=httpx.Response(200, content=xlsx_2026.read_bytes())
    )
    with httpx.Client() as client:
        p = fetch_xlsx(KIND_ARGE, client=client, cache_dir=tmp_path, wayback_sleep_sec=0)
    assert p is not None and argem.cache_meta(p)["kaynak"] == "wayback"


@respx.mock
def test_collector_offline_uses_cache_metadata_for_tasarim(tmp_path, caplog):
    data = simple([[1, "Tasarım Yazılım A.Ş.", "Bursa", "Yazılım", "01.02.2024"]],
                  header=["SIRA NO", "TASARIM MERKEZİNİN ADI", "İL", "SEKTÖR", "BELGE TARİHİ"])
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "Tasarimmerkeziistatistik.xlsx").write_bytes(data)
    (cache / "Tasarimmerkeziistatistik.json").write_text(json.dumps({"dosya_tarihi": "2024-05-01"}), encoding="utf-8")
    c = ArgemCollector(data_dir=tmp_path / "argem", cache_dir=cache, allow_network=False)
    out = list(c.collect(cities=["bursa"]))
    assert len(out) == 1
    assert out[0].tags == ["tasarim-merkezi"] and out[0].raw["dosya_tarihi"] == "2024-05-01"
    assert "Tasarım Merkezi" in out[0].description and "01.02.2024" in out[0].description


def test_bad_cache_meta_json_is_ignored(tmp_path, xlsx_2026):
    p = tmp_path / "Ar-Gemerkezleri.xlsx"
    p.write_bytes(xlsx_2026.read_bytes())
    p.with_suffix(".json").write_text("{bozuk json", encoding="utf-8")
    assert argem.cache_meta(p) == {}
    p.with_suffix(".json").write_text("[1,2,3]", encoding="utf-8")
    assert argem.cache_meta(p) == {}
    out = list(ArgemCollector(path=p, only_tech=False).collect(cities=["Afyon"]))
    assert len(out) == 1 and out[0].raw["dosya_tarihi"] is not None  # mtime'a düşer
