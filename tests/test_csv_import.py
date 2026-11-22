"""csv_import.py testleri: CSV (ayraç/kodlama), XLSX, Türkçe başlıklar, okul kaynağı."""
from __future__ import annotations

from pathlib import Path

import pytest

from stajbot.collectors import csv_import as ci
from stajbot.collectors import ingest as ing
from stajbot.models import Company

FIX = Path(__file__).parent / "fixtures" / "cli" / "sablon.csv"


def test_reads_template_fixture():
    raws = ci.read_file(FIX)
    assert [r.name for r in raws] == ["Örnek Yazılım A.Ş.", "Konya Bilişim Ltd.", "Şehirsiz Firma"]
    a = raws[0]
    assert a.website == "https://www.ornekyazilim.com" and a.emails == ["kariyer@ornekyazilim.com"]
    assert a.city == "İstanbul" and a.district == "Maslak" and a.tags == ["fintech", "odeme"]
    assert a.raw["notes"] == "Okul listesinden" and a.source == "csv" and a.school_protocol is False
    assert raws[1].website == "https://konyabilisim.com.tr" and raws[1].raw["contact_name"] == "Ayşe Yılmaz"
    assert raws[0].source_url.startswith("file:///")


def test_school_source_sets_flag_and_tag():
    raws = ci.read_file(FIX, source="school", default_city="Konya")
    assert all(r.school_protocol and "okul-protokol" in r.tags for r in raws)
    assert raws[2].city == "Konya"  # şehirsiz satır varsayılanı alır


def test_turkish_headers_semicolon_cp1254(tmp_path):
    p = tmp_path / "liste.csv"
    text = "Şirket Adı;Web Sitesi;E-posta;Şehir;Telefon;Etiketler\nAcme Ltd.;acme.com.tr;ik@acme.com.tr, info@acme.com.tr;Konya;0332;a|b\n;;;;\n"
    p.write_bytes(text.encode("cp1254"))
    raws = ci.read_file(p)
    assert len(raws) == 1
    r = raws[0]
    assert r.name == "Acme Ltd." and r.website == "https://acme.com.tr" and r.emails == ["ik@acme.com.tr", "info@acme.com.tr"]
    assert r.tags == ["a", "b"] and r.phone == "0332"


def test_tab_and_quoted_newline(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text('name\taddress\tcity\n"Çok Satırlı"\t"Sok. 1\nKat 2"\tAnkara\n', encoding="utf-8")
    raws = ci.read_file(p)
    assert len(raws) == 1 and raws[0].address == "Sok. 1 Kat 2"


def test_missing_name_column_raises(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="şirket adı sütunu"):
        ci.read_file(p)


def test_empty_file(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("", encoding="utf-8")
    assert ci.read_file(p) == []


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        ci.read_file(Path("yok/boyle/bir.csv"))


def test_xlsx_with_header_on_later_row(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Okul Protokol Listesi"])
    ws.append([])
    ws.append(["Firma", "Şehir", "E-posta", "Web"])
    ws.append(["Delta Robotik", "Eskişehir", "staj@delta.com.tr", "www.delta.com.tr"])
    ws.append([None, None, None, None])
    p = tmp_path / "okul.xlsx"
    wb.save(p)
    raws = ci.read_file(p, source="school")
    assert len(raws) == 1
    assert raws[0].name == "Delta Robotik" and raws[0].city == "Eskişehir" and raws[0].website == "https://www.delta.com.tr"
    assert raws[0].school_protocol


def test_end_to_end_with_ingest_is_idempotent(session):
    raws = ci.read_file(FIX, source="school")
    r1 = ing.ingest(raws, session)
    session.commit()
    r2 = ing.ingest(ci.read_file(FIX, source="school"), session)
    assert r1.created == 2 and r1.skipped_city == 1 and r2.created == 0 and r2.updated == 2
    rows = session.query(Company).all()
    assert len(rows) == 2 and all(r.school_protocol for r in rows)


def test_template_text_header():
    assert ci.template_text().splitlines()[0] == ",".join(ci.TEMPLATE_COLUMNS)
