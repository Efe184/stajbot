"""Ar-Ge / Tasarım Merkezleri toplayıcısı (stajbot.collectors.argem) testleri.

Çevrimdışı testler ``tests/fixtures/argem/`` altındaki iki gerçek XLSX kopyasını kullanır:
* ``Ar-Gemerkezleri_2026-02_archived.xlsx`` — 1368 satır, sütunlar: SIRA NO | ADI | İL | SEKTÖR | TARİH
* ``ArGeMerkezleriSubat2020_archived.xlsx`` — 1236 satır, sütunlar: SIRA NO | ADI | SEKTÖR | İL | TARİH (nbsp gürültülü)
"""
from __future__ import annotations

import io
import json
import logging
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
    ArgemRecord,
    canonical_sector,
    clean_company_name,
    dedupe_records,
    detect_columns,
    detect_kind,
    fetch_xlsx,
    filter_records_by_cities,
    fold_header,
    is_tech_record,
    is_tech_sector,
    is_xlsx_bytes,
    parse_xlsx,
    parse_xlsx_records,
    record_to_raw,
    wayback_latest_capture,
)
from stajbot.collectors.base import RawCompany
from stajbot.enums import TARGET_CITIES, is_target_city

TARGETS_LOWER = ["konya", "ankara", "istanbul", "izmir", "eskisehir", "bursa", "sakarya", "antalya", "mersin", "afyon"]


# --- fikstürler -------------------------------------------------------------------

@pytest.fixture(scope="module")
def argem_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "argem"


@pytest.fixture(scope="module")
def xlsx_2026(argem_dir) -> Path:
    return argem_dir / "Ar-Gemerkezleri_2026-02_archived.xlsx"


@pytest.fixture(scope="module")
def xlsx_2020(argem_dir) -> Path:
    return argem_dir / "ArGeMerkezleriSubat2020_archived.xlsx"


@pytest.fixture(scope="module")
def records_2026(xlsx_2026) -> list[ArgemRecord]:
    return parse_xlsx_records(xlsx_2026)


@pytest.fixture(scope="module")
def records_2020(xlsx_2020) -> list[ArgemRecord]:
    return parse_xlsx_records(xlsx_2020)


def make_xlsx(header: list, rows: list[list], *, leading_rows: int = 0) -> bytes:
    """Bellekte küçük bir çalışma kitabı üretir (başlık + satırlar)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sayfa1"
    for _ in range(leading_rows):
        ws.append(["Bakanlık listesi", None, None])
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- başlık eşleme -------------------------------------------------------------------

def test_fold_header_turkish_and_nbsp():
    assert fold_header("AR-GE MERKEZİNİN ADI   ") == "AR-GE MERKEZININ ADI"
    assert fold_header("\xa0İL ") == "IL"
    assert fold_header("SEKTÖR") == "SEKTOR"
    assert fold_header(None) == ""


def test_detect_columns_2026_order():
    cols = detect_columns(["SIRA NO", "AR-GE MERKEZİNİN ADI   ", "İL", "SEKTÖR", "BELGE TARİHİ"])
    assert cols is not None
    assert (cols.order, cols.name, cols.city, cols.sector, cols.date) == (0, 1, 2, 3, 4)


def test_detect_columns_2020_order_swapped():
    cols = detect_columns(["SIRA NO ", "AR-GE MERKEZİNİN ADI   ", "SEKTÖR", "İL", "BELGE TARİHİ"])
    assert cols is not None
    assert (cols.order, cols.name, cols.city, cols.sector, cols.date) == (0, 1, 3, 2, 4)


def test_detect_columns_tasarim_header_and_kind():
    header = ["SIRA NO", "TASARIM MERKEZİNİN ADI", "İL", "SEKTÖR", "BELGE TARİHİ"]
    cols = detect_columns(header)
    assert cols is not None and cols.name == 1 and cols.city == 2
    assert detect_kind(header) == KIND_TASARIM
    assert detect_kind(["SIRA NO", "AR-GE MERKEZİNİN ADI", "İL"]) == KIND_ARGE
    assert detect_kind(["NO", "FİRMA ADI", "İL"], "Tasarimmerkeziistatistik.xlsx") == KIND_TASARIM


def test_detect_columns_missing_city_returns_none():
    assert detect_columns(["SIRA NO", "AR-GE MERKEZİNİN ADI", "SEKTÖR"]) is None
    assert detect_columns([None, None]) is None


# --- metin yardımcıları ---------------------------------------------------------------

def test_clean_company_name_strips_trailing_parenthetical():
    assert clean_company_name("Savronik Elektronik Sanayi ve Ticaret A.Ş. (Eskişehir)") == "Savronik Elektronik Sanayi ve Ticaret A.Ş."
    assert (
        clean_company_name("STM Savunma Teknolojileri Mühendislik ve Ticaret. A.Ş. (Ankara İleri Teknolojiler Ofisi)")
        == "STM Savunma Teknolojileri Mühendislik ve Ticaret. A.Ş."
    )
    # Ortadaki parantez korunur, nbsp temizlenir, boş kalırsa orijinal döner
    assert clean_company_name("Abc (Grup) Yazılım A.Ş.") == "Abc (Grup) Yazılım A.Ş."
    assert clean_company_name("2M\xa0Kablo\xa0San. ve Tic. A.Ş.") == "2M Kablo San. ve Tic. A.Ş."
    assert clean_company_name("(Sadece Not)") == "(Sadece Not)"


def test_canonical_sector_folds_drifted_labels():
    assert canonical_sector("Elektrik-Elektronik") == "Elektronik"
    assert canonical_sector("Gıda") == "Gıda Sanayi"
    assert canonical_sector("Plastik-Kauçuk") == "Lastik-Plastik-Kauçuk"
    assert canonical_sector("Otomotiv Yan sanayi") == "Otomotiv Yan Sanayi"
    assert canonical_sector("Kimya ") == "Kimya"
    assert canonical_sector(None) is None
    assert canonical_sector("  ") is None


@pytest.mark.parametrize(
    "sector,expected",
    [
        ("Yazılım", True),
        ("Bilgisayar ve İletişim Teknolojileri", True),
        ("Elektronik", True),
        ("Elektrik-Elektronik", True),
        ("Savunma Sanayi", True),
        ("Telekomünikasyon", True),
        ("Otomotiv Yan Sanayi", True),
        ("Otomotiv Yan sanayi", True),
        ("Havacılık", True),
        ("Gıda Sanayi", False),
        ("Tekstil", False),
        ("Kimya", False),
        ("Makine ve Teçhizat İmalatı", False),
        ("Enerji", False),
        (None, False),
        ("", False),
    ],
)
def test_is_tech_sector_core(sector, expected):
    assert is_tech_sector(sector) is expected


def test_is_tech_sector_wide_tier():
    assert is_tech_sector("Makine ve Teçhizat İmalatı", wide=True)
    assert is_tech_sector("Enerji", wide=True)
    assert is_tech_sector("Mühendislik/Mimarlık Faaliyetleri", wide=True)
    assert not is_tech_sector("Tekstil", wide=True)
    assert not is_tech_sector("Gıda Sanayi", wide=True)


def test_is_tech_sector_unknown_label_keyword_fallback():
    # Tasarım dosyasında farklı etiketler olabilir; anahtar kelime ile yakalanır
    assert is_tech_sector("Yazılım ve Bilişim Hizmetleri")
    assert is_tech_sector("Savunma ve Havacılık")
    assert not is_tech_sector("Mobilya Tasarımı")


def _rec(name: str, sector: str | None, city: str = "Konya") -> ArgemRecord:
    return ArgemRecord(
        name=name, name_clean=clean_company_name(name), city=city, sector=sector,
        sector_canonical=canonical_sector(sector), cert_date=None, kind=KIND_ARGE,
    )


def test_is_tech_record_uses_company_name_hint():
    assert is_tech_record(_rec("Akın Yazılım Bilgisayar İth. İhr. Ltd. Şti.", "Makine ve Teçhizat İmalatı"))
    assert is_tech_record(_rec("Dof Robotik Sanayi A.Ş.", "Makine ve Teçhizat İmalatı"))
    assert not is_tech_record(_rec("Kızılay İçecek A.Ş.", "Gıda Sanayi"))


# --- gerçek fikstürler: 2026 ---------------------------------------------------------------

def test_parse_2026_row_count_and_types(records_2026):
    assert len(records_2026) == 1368
    first = records_2026[0]
    assert first.sira_no == 1
    assert first.kind == KIND_ARGE
    assert isinstance(first.cert_date, date) and not isinstance(first.cert_date, datetime)
    assert all(isinstance(r.cert_date, date) for r in records_2026)
    assert all(isinstance(r.sira_no, int) for r in records_2026)
    assert records_2026[-1].sira_no == 1368
    assert first.file_name == "Ar-Gemerkezleri_2026-02_archived.xlsx"
    assert first.file_date is not None  # dosya mtime
    assert first.source_url == OFFICIAL_URLS[KIND_ARGE]


def test_parse_2026_city_counts(records_2026):
    def count(city: str) -> int:
        return sum(1 for r in records_2026 if r.city == city)

    assert count("İstanbul") == 432
    assert count("Ankara") == 165
    assert count("Bursa") == 135
    assert count("İzmir") == 106
    assert count("Konya") == 25
    assert count("Afyonkarahisar") == 1
    assert count("Mersin") == 11


def test_parse_2026_sample_rows(records_2026):
    by_no = {r.sira_no: r for r in records_2026}
    r = by_no[707]
    assert r.name == "Kızılay İçecek A.Ş." and r.city == "Afyonkarahisar" and r.sector == "Gıda Sanayi"
    assert r.cert_date == date(2020, 6, 4)
    r = by_no[1064]
    assert r.name == "Savronik Elektronik Sanayi ve Ticaret A.Ş. (Eskişehir)"
    assert r.name_clean == "Savronik Elektronik Sanayi ve Ticaret A.Ş."
    assert r.site_note == "Eskişehir" and r.city == "Eskişehir"
    r = by_no[1119]
    assert r.name_clean == "STM Savunma Teknolojileri Mühendislik ve Ticaret. A.Ş."
    assert r.site_note == "Ankara İleri Teknolojiler Ofisi"
    r = by_no[126]
    assert r.name == "Aselsan Konya Silah Sistemleri Anonim Şirketi" and r.city == "Konya" and r.cert_date == date(2021, 10, 12)


def test_2026_target_city_and_core_sector_counts(records_2026):
    target = filter_records_by_cities(records_2026, TARGETS_LOWER)
    assert len(target) == 941
    assert sum(1 for r in target if is_tech_sector(r.sector)) == 486
    # ad ipucu birkaç satır daha katar ama çekirdekten az olamaz
    assert sum(1 for r in target if is_tech_record(r)) >= 486
    assert sum(1 for r in target if is_tech_sector(r.sector, wide=True)) > 486


def test_filter_records_by_cities_accepts_aliases(records_2026):
    assert len(filter_records_by_cities(records_2026, ["afyon"])) == 1
    assert len(filter_records_by_cities(records_2026, ["ISTANBUL"])) == 432
    assert len(filter_records_by_cities(records_2026, ["istanbul", "ankara"])) == 432 + 165
    assert len(filter_records_by_cities(records_2026, None)) == 1368
    assert len(filter_records_by_cities(records_2026, [])) == 1368


# --- gerçek fikstürler: 2020 (sütun sırası farklı, nbsp gürültüsü) -------------------------------

def test_parse_2020_row_count_and_column_order(records_2020):
    assert len(records_2020) == 1236
    first = records_2020[0]
    assert first.name == "2M Kablo San. ve Tic. A.Ş."  # nbsp -> boşluk
    assert first.city == "İstanbul"  # 'İstanbul ' -> strip
    assert first.sector == "Elektrik-Elektronik" and first.sector_canonical == "Elektronik"
    assert first.cert_date == date(2017, 6, 7)
    assert all("\xa0" not in (r.city or "") and not (r.city or "").endswith(" ") for r in records_2020)
    assert sum(1 for r in records_2020 if r.city == "İstanbul") == 427
    assert sum(1 for r in records_2020 if r.city == "Konya") == 25
    # Çankırı\xa0 gibi iller temizlenmiş olmalı
    assert any(r.city == "Çankırı" for r in records_2020)


def test_2020_sector_canonical_folds_old_labels(records_2020):
    canon = {r.sector_canonical for r in records_2020}
    assert "Elektrik-Elektronik" not in canon and "Elektronik" in canon
    assert "Gıda" not in canon and "Gıda Sanayi" in canon
    assert "Otomotiv Yan sanayi" not in canon


# --- tekilleştirme ve RawCompany dönüşümü -------------------------------------------------------

def test_dedupe_records_merges_same_firm_same_city(records_2026):
    deduped = dedupe_records(records_2026)
    assert len(deduped) < len(records_2026)
    keys = [(r.name_clean, r.city) for r in deduped]
    assert len(keys) == len(set(keys))
    # Savronik hem Ankara hem Eskişehir'de: iki ayrı kayıt kalır
    sav = [r for r in deduped if r.name_clean.startswith("Savronik")]
    assert {r.city for r in sav} == {"Ankara", "Eskişehir"}
    # Abdi İbrahim'in iki şubesi tek kayıt olur, notlar birleşir
    abdi = [r for r in deduped if r.name_clean.startswith("Abdi İbrahim İlaç")]
    assert len(abdi) == 1 and ";" in (abdi[0].site_note or "")


def test_record_to_raw_fields(records_2026):
    rec = next(r for r in records_2026 if r.sira_no == 1064)
    raw = record_to_raw(rec)
    assert isinstance(raw, RawCompany)
    assert raw.source == "argem"
    assert raw.name == "Savronik Elektronik Sanayi ve Ticaret A.Ş."
    assert raw.city == "Eskişehir"
    assert raw.sector == "Elektronik"
    assert raw.tags == ["arge-merkezi"]
    assert raw.is_arge_center is True
    assert raw.source_url == OFFICIAL_URLS[KIND_ARGE]
    assert "Ar-Ge Merkezi" in raw.description and "09.03.2017" in raw.description
    assert raw.raw["firma"] == rec.name and raw.raw["firma_temiz"] == raw.name
    assert raw.raw["belge_tarihi"] == "2017-03-09" and raw.raw["sira_no"] == 1064
    json.dumps(raw.raw, ensure_ascii=False)  # JSON'a yazılabilir olmalı


def test_record_to_raw_tasarim_tag():
    rec = ArgemRecord(
        name="Örnek Tasarım A.Ş.", name_clean="Örnek Tasarım A.Ş.", city="İzmir ", sector="Yazılım",
        sector_canonical="Yazılım", cert_date=date(2024, 1, 2), kind=KIND_TASARIM,
    )
    raw = record_to_raw(rec)
    assert raw.tags == ["tasarim-merkezi"] and raw.is_arge_center is True
    assert raw.city == "İzmir" and raw.source_url == OFFICIAL_URLS[KIND_TASARIM]
    assert "Tasarım Merkezi" in raw.description


def test_parse_xlsx_bytes_and_path_equivalent(xlsx_2026):
    from_path = parse_xlsx(xlsx_2026, dedupe=False)
    from_bytes = parse_xlsx(xlsx_2026.read_bytes(), dedupe=False)
    assert len(from_path) == len(from_bytes) == 1368
    assert [c.name for c in from_path] == [c.name for c in from_bytes]
    assert from_bytes[0].raw["dosya"] is None and from_path[0].raw["dosya"] is not None


def test_parse_xlsx_filters(xlsx_2026):
    konya = parse_xlsx(xlsx_2026, cities=["konya"], only_tech=False)
    assert len(konya) == 25 and all(c.city == "Konya" for c in konya)
    konya_tech = parse_xlsx(xlsx_2026, cities=["konya"], only_tech=True)
    assert 0 < len(konya_tech) < len(konya)
    assert any("Aselsan Konya" in c.name for c in konya_tech)
    afyon = parse_xlsx(xlsx_2026, cities=["afyon"], only_tech=True)
    assert afyon == []  # tek merkez gıda sektörü


# --- sentetik dosyalar -------------------------------------------------------------------------

def test_parse_synthetic_tasarim_file_with_header_offset_and_text_dates():
    data = make_xlsx(
        ["SIRA NO", "TASARIM MERKEZİNİN ADI", "SEKTÖR", "İL", "BELGE TARİHİ"],
        [
            [1, "Alfa Tasarım ve Yazılım A.Ş. (Konya Teknokent)", "Yazılım", "Konya\xa0", "12.03.2023"],
            [2, "Beta Mobilya Tasarım Ltd.", "Mobilya", "Bursa", datetime(2022, 5, 1)],
            ["TOPLAM", None, None, None, None],
            [None, None, None, None, None],
        ],
        leading_rows=2,
    )
    recs = parse_xlsx_records(data)
    assert len(recs) == 2
    assert recs[0].kind == KIND_TASARIM and recs[0].row_no == 4
    assert recs[0].city == "Konya" and recs[0].site_note == "Konya Teknokent"
    assert recs[0].cert_date == date(2023, 3, 12) and recs[1].cert_date == date(2022, 5, 1)
    raws = parse_xlsx(data, only_tech=True)
    assert len(raws) == 1 and raws[0].tags == ["tasarim-merkezi"]


def test_parse_synthetic_without_header_returns_empty(caplog):
    data = make_xlsx(["A", "B", "C"], [[1, "x", "y"]])
    with caplog.at_level(logging.WARNING, logger="stajbot.collectors.argem"):
        assert parse_xlsx_records(data) == []
    assert any("başlı" in m for m in caplog.messages)


# --- collector ---------------------------------------------------------------------------------

def test_collector_protocol_fields(xlsx_2026):
    c = ArgemCollector(path=xlsx_2026)
    assert c.name == "argem"
    out = list(c.collect(cities=None))
    assert out and all(isinstance(x, RawCompany) for x in out)
    assert all(x.is_arge_center and x.source == "argem" and x.tags == ["arge-merkezi"] for x in out)
    assert c.last_files == [xlsx_2026]


def test_collector_city_filter_and_only_tech(xlsx_2026):
    out = list(ArgemCollector(path=xlsx_2026).collect(cities=list(TARGET_CITIES)))
    assert 486 <= len(out) <= 520
    assert all(is_target_city(x.city) for x in out)
    assert not any(x.name.startswith("Kızılay İçecek") for x in out)
    cities = {x.city for x in out}
    assert {"İstanbul", "Ankara", "Bursa", "İzmir", "Konya", "Eskişehir", "Sakarya"} <= cities
    # tekil (ad, şehir)
    keys = [(x.name, x.city) for x in out]
    assert len(keys) == len(set(keys))


def test_collector_only_tech_false_keeps_everything(xlsx_2026):
    out = list(ArgemCollector(path=xlsx_2026, only_tech=False).collect(cities=["Afyonkarahisar"]))
    assert len(out) == 1 and out[0].name == "Kızılay İçecek A.Ş." and out[0].sector == "Gıda Sanayi"


def test_collector_wide_sectors_adds_antalya(xlsx_2026):
    strict = list(ArgemCollector(path=xlsx_2026).collect(cities=["Antalya"]))
    wide = list(ArgemCollector(path=xlsx_2026, wide_sectors=True).collect(cities=["Antalya"]))
    assert len(wide) > len(strict)


def test_collector_directory_path_reads_all_xlsx(tmp_path, xlsx_2026, xlsx_2020):
    (tmp_path / "a.xlsx").write_bytes(xlsx_2026.read_bytes())
    (tmp_path / "b.xlsx").write_bytes(xlsx_2020.read_bytes())
    (tmp_path / "~$b.xlsx").write_bytes(b"lock")  # Excel geçici dosyası atlanır
    c = ArgemCollector(path=tmp_path, only_tech=False)
    out = list(c.collect(cities=["Konya"]))
    assert len(c.last_files) == 2
    # iki dosyadaki Konya firmaları birleşir ve tekilleşir: 25'ten fazla ama 50'den az
    assert 25 < len(out) < 50


def test_collector_missing_path_returns_empty(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="stajbot.collectors.argem"):
        out = list(ArgemCollector(path=tmp_path / "yok.xlsx").collect())
    assert out == []
    assert any("bulunamadı" in m for m in caplog.messages)


def test_collector_corrupt_file_is_skipped(tmp_path, caplog):
    bad = tmp_path / "bozuk.xlsx"
    bad.write_bytes(b"bu bir xlsx degil")
    with caplog.at_level(logging.WARNING, logger="stajbot.collectors.argem"):
        out = list(ArgemCollector(path=bad).collect())
    assert out == []
    assert any("okunamadı" in m for m in caplog.messages)


def test_collector_no_files_no_network_logs_instructions(tmp_path, caplog):
    c = ArgemCollector(data_dir=tmp_path / "argem", cache_dir=tmp_path / "cache", allow_network=False)
    with caplog.at_level(logging.WARNING, logger="stajbot.collectors.argem"):
        out = list(c.collect())
    assert out == []
    joined = "\n".join(caplog.messages)
    assert "tarayıcı" in joined and OFFICIAL_URLS[KIND_ARGE] in joined


def test_collector_prefers_manual_data_dir(tmp_path, xlsx_2026):
    data_dir = tmp_path / "argem"
    data_dir.mkdir()
    (data_dir / "Ar-Gemerkezleri.xlsx").write_bytes(xlsx_2026.read_bytes())
    c = ArgemCollector(data_dir=data_dir, cache_dir=tmp_path / "cache", allow_network=False, only_tech=False)
    out = list(c.collect(cities=["Konya"]))
    assert len(out) == 25 and c.last_files == [data_dir / "Ar-Gemerkezleri.xlsx"]


def test_collector_uses_cache_when_offline(tmp_path, xlsx_2026):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "Ar-Gemerkezleri.xlsx").write_bytes(xlsx_2026.read_bytes())
    (cache / "Ar-Gemerkezleri.json").write_text(json.dumps({"dosya_tarihi": "2026-02-06"}), encoding="utf-8")
    c = ArgemCollector(data_dir=tmp_path / "argem", cache_dir=cache, allow_network=False, include_tasarim=False)
    out = list(c.collect(cities=["Konya"]))
    assert out and all(x.raw["dosya_tarihi"] == "2026-02-06" for x in out)


# --- indirme (respx ile sahte ağ) ----------------------------------------------------------------

WAF_STUB = b"<html><head><title>Request Rejected</title></head><body>The requested URL was rejected. support ID: 1</body></html>"


def test_is_xlsx_bytes():
    assert not is_xlsx_bytes(None)
    assert not is_xlsx_bytes(b"")
    assert not is_xlsx_bytes(WAF_STUB)
    assert not is_xlsx_bytes(b"PK\x03\x04" + b"0" * 10)  # çok küçük
    assert is_xlsx_bytes(b"PK\x03\x04" + b"0" * 5000)


@respx.mock
def test_wayback_latest_capture_retries_and_picks_newest():
    route = respx.get(argem.WAYBACK_CDX_URL)
    route.side_effect = [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(
            200,
            json=[
                ["timestamp", "original", "statuscode", "mimetype", "length"],
                ["20220310120000", "https://www.sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx", "200",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "90000"],
                ["20260206163845", "https://sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx", "200",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "95000"],
                ["20260730000000", "https://www.sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx", "200", "text/html", "244"],
            ],
        ),
    ]
    with httpx.Client() as client:
        cap = wayback_latest_capture(OFFICIAL_URLS[KIND_ARGE], client, retries=3, sleep_sec=0)
    assert cap == ("20260206163845", "https://sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx")
    assert route.call_count == 2
    params = route.calls[0].request.url.params
    assert params["url"] == "sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx"
    assert params["output"] == "json"
    assert set(params.get_list("filter")) == {"statuscode:200", "mimetype:.*sheet.*"}


@respx.mock
def test_wayback_latest_capture_gives_up_after_retries():
    respx.get(argem.WAYBACK_CDX_URL).mock(return_value=httpx.Response(503))
    with httpx.Client() as client:
        assert wayback_latest_capture(OFFICIAL_URLS[KIND_ARGE], client, retries=2, sleep_sec=0) is None


@respx.mock
def test_wayback_latest_capture_empty_body():
    respx.get(argem.WAYBACK_CDX_URL).mock(return_value=httpx.Response(200, text=""))
    with httpx.Client() as client:
        assert wayback_latest_capture(OFFICIAL_URLS[KIND_ARGE], client, retries=1, sleep_sec=0) is None


@respx.mock
def test_fetch_xlsx_direct_blocked_falls_back_to_wayback(tmp_path, xlsx_2026):
    direct = respx.get(OFFICIAL_URLS[KIND_ARGE]).mock(return_value=httpx.Response(200, content=WAF_STUB, headers={"content-type": "text/html"}))
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
    wb = respx.get("https://web.archive.org/web/20260206163845id_/https://sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx").mock(
        return_value=httpx.Response(200, content=xlsx_2026.read_bytes())
    )
    cache = tmp_path / "cache"
    with httpx.Client() as client:
        p = fetch_xlsx(KIND_ARGE, client=client, cache_dir=cache, wayback_sleep_sec=0)
    assert direct.called and wb.called
    assert p == cache / "Ar-Gemerkezleri.xlsx" and p.exists()
    meta = argem.cache_meta(p)
    assert meta["kaynak"] == "wayback" and meta["dosya_tarihi"] == "2026-02-06"
    assert len(parse_xlsx_records(p)) == 1368

    # ikinci çağrı taze önbelleği kullanır, ağa çıkmaz
    with httpx.Client() as client:
        p2 = fetch_xlsx(KIND_ARGE, client=client, cache_dir=cache)
    assert p2 == p and direct.call_count == 1 and wb.call_count == 1


@respx.mock
def test_fetch_xlsx_direct_success(tmp_path, xlsx_2026):
    respx.get(OFFICIAL_URLS[KIND_ARGE]).mock(return_value=httpx.Response(200, content=xlsx_2026.read_bytes()))
    cdx = respx.get(argem.WAYBACK_CDX_URL).mock(return_value=httpx.Response(200, json=[]))
    with httpx.Client() as client:
        p = fetch_xlsx(KIND_ARGE, client=client, cache_dir=tmp_path)
    assert p is not None and argem.cache_meta(p)["kaynak"] == "dogrudan"
    assert not cdx.called


@respx.mock
def test_fetch_xlsx_all_fail_returns_none_or_stale(tmp_path):
    respx.get(OFFICIAL_URLS[KIND_ARGE]).mock(side_effect=httpx.ConnectError("boom"))
    respx.get(argem.WAYBACK_CDX_URL).mock(return_value=httpx.Response(200, json=[]))
    with httpx.Client() as client:
        assert fetch_xlsx(KIND_ARGE, client=client, cache_dir=tmp_path, wayback_sleep_sec=0) is None
    # eski önbellek varsa o döner
    stale = tmp_path / "Ar-Gemerkezleri.xlsx"
    stale.write_bytes(b"PK" + b"0" * 3000)
    with httpx.Client() as client:
        assert fetch_xlsx(KIND_ARGE, client=client, cache_dir=tmp_path, max_age_days=0, wayback_sleep_sec=0) == stale


@respx.mock
def test_collector_end_to_end_with_mocked_network(tmp_path, xlsx_2026):
    respx.get(OFFICIAL_URLS[KIND_ARGE]).mock(return_value=httpx.Response(200, content=xlsx_2026.read_bytes()))
    respx.get(OFFICIAL_URLS[KIND_TASARIM]).mock(return_value=httpx.Response(200, content=WAF_STUB))
    respx.get(argem.WAYBACK_CDX_URL).mock(return_value=httpx.Response(200, json=[]))
    c = ArgemCollector(data_dir=tmp_path / "argem", cache_dir=tmp_path / "cache", client=httpx.Client(), wayback_sleep_sec=0)
    try:
        out = list(c.collect(cities=["Konya"]))
    finally:
        c.client.close()
    assert out and all(x.city == "Konya" for x in out)
    assert (tmp_path / "cache" / "Ar-Gemerkezleri.xlsx").exists()
    assert not (tmp_path / "cache" / "Tasarimmerkeziistatistik.xlsx").exists()


# --- canlı ağ (varsayılan olarak atlanır) ------------------------------------------------------------

@pytest.mark.network
def test_live_fetch_arge_list(tmp_path):
    """Gerçek indirme: doğrudan (WAF muhtemelen engeller) ya da Wayback'teki en yeni gerçek kopya."""
    p = fetch_xlsx(KIND_ARGE, cache_dir=tmp_path)
    assert p is not None, "Ne doğrudan ne Wayback üzerinden Ar-Ge listesi indirilemedi"
    assert is_xlsx_bytes(p.read_bytes())
    recs = parse_xlsx_records(p)
    assert len(recs) > 1000
    assert sum(1 for r in recs if r.city == "İstanbul") > 300
    assert all(isinstance(r.cert_date, date) for r in recs[:50])
