"""ingest + csv_import için ek (saldırgan) testler: tekilleştirme köşe durumları, dolu alan koruması,
savepoint geri alma, binlerce kayıtta N+1 sorgu yokluğu, bozuk kayıt toleransı, CSV/XLSX kodlama ve başlık
varyantları. Ağa çıkmaz; bellek içi veritabanı kullanır.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, select

from stajbot.collectors import csv_import as ci
from stajbot.collectors import ingest as ing
from stajbot.collectors.base import RawCompany
from stajbot.db import get_sessionmaker
from stajbot.enums import ContactKind, SizeBucket
from stajbot.models import Company, CompanySource, Contact, Event


def _raw(**kw) -> RawCompany:
    base = dict(name="Alfa Yazılım A.Ş.", source="teknopark", city="Konya", website="https://www.alfa.com.tr")
    base.update(kw)
    return RawCompany(**base)


def _one(session) -> Company:
    return session.scalars(select(Company)).one()


# --- web sitesi / domain anahtarı -------------------------------------------------------------

def test_multi_url_cell_keeps_first_site_and_domain(session):
    ing.ingest([_raw(website="www.alfa.com.tr, www.alfa.com")], session)
    c = _one(session)
    assert c.website == "https://www.alfa.com.tr" and c.domain == "alfa.com.tr"
    # aynı şirket tek siteyle yeniden gelirse eşleşir
    rep = ing.ingest([_raw(name="ALFA", website="alfa.com.tr/")], session)
    assert (rep.created, rep.updated) == (0, 1)


@pytest.mark.parametrize("site", ["yok", "-", "N/A", "www", "http://", "   "])
def test_junk_website_values_do_not_crash_or_become_domain(session, site):
    rep = ing.ingest([_raw(website=site, emails=["info@alfa.com.tr"])], session)
    assert rep.errors == [] and rep.created == 1
    c = _one(session)
    assert c.website is None and c.domain == "alfa.com.tr"  # anahtar e-postadan gelir


def test_email_domain_key_skips_generic_and_matches_later_website(session):
    ing.ingest([_raw(website=None, emails=["ahmet@gmail.com", "ik@alfa.com.tr"])], session)
    rep = ing.ingest([_raw(name="Tamamen Farklı Ad", city="Ankara", website="https://alfa.com.tr/tr")], session)
    assert (rep.created, rep.updated) == (0, 1)
    c = _one(session)
    assert c.domain == "alfa.com.tr" and c.name == "Alfa Yazılım A.Ş." and c.city == "Konya"
    assert sorted(k.email for k in c.contacts) == ["ahmet@gmail.com", "ik@alfa.com.tr"]


# --- dolu alanlar üzerine yazılmaz, bayraklar OR ------------------------------------------------

def test_filled_fields_are_never_overwritten_and_flags_stay_true(session):
    first = _raw(
        district="Selçuklu", address="Teknokent 1", phone="0332 1", sector="Savunma", description="ilk",
        linkedin_url="https://linkedin.com/company/alfa", teknopark_name="Konya Teknokent",
        internship_posting_url="https://alfa.com.tr/staj", employee_count=50,
        is_arge_center=True, is_teknopark=True, school_protocol=True, emails=["ik@alfa.com.tr"],
    )
    ing.ingest([first], session)
    second = _raw(
        source="argem", website="https://alfa.com.tr/other", city="Ankara",
        district="Çankaya", address="Başka", phone="0312", sector="Oyun", description="ikinci",
        linkedin_url="https://linkedin.com/company/baska", teknopark_name="Başka", internship_posting_url="x",
        employee_count=5000, is_arge_center=False, is_teknopark=False, school_protocol=False,
    )
    rep = ing.ingest([second], session)
    assert (rep.created, rep.updated) == (0, 1)
    c = _one(session)
    assert c.website == "https://www.alfa.com.tr" and c.city == "Konya"
    assert (c.district, c.address, c.phone, c.sector, c.description) == ("Selçuklu", "Teknokent 1", "0332 1", "Savunma", "ilk")
    assert c.linkedin_url == "https://linkedin.com/company/alfa" and c.teknopark_name == "Konya Teknokent"
    assert c.internship_posting_url == "https://alfa.com.tr/staj"
    assert c.employee_count == 50 and c.size_bucket == SizeBucket.s11_50
    assert c.is_arge_center and c.is_teknopark and c.school_protocol and c.has_internship_posting
    assert sorted(c.source_names) == ["argem", "teknopark"]


def test_employee_count_strings_are_parsed_leniently(session):
    rep = ing.ingest([_raw(employee_count="120+")], session)  # type: ignore[arg-type]
    assert rep.errors == [] and rep.created == 1
    assert _one(session).employee_count == 120 and _one(session).size_bucket == SizeBucket.s51_200
    rep2 = ing.ingest([_raw(name="Beta", website="https://beta.com", employee_count="bilinmiyor")], session)  # type: ignore[arg-type]
    assert rep2.errors == [] and rep2.created == 1
    beta = session.scalar(select(Company).where(Company.name == "Beta"))
    assert beta.employee_count is None and beta.size_bucket == SizeBucket.unknown


# --- şehir --------------------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["KONYA ", " konya", "Konya/Selçuklu", "Konya - Meram", "konya,"])
def test_city_spelling_variants_dedupe_by_name(session, variant):
    ing.ingest([_raw(website=None)], session)
    rep = ing.ingest([_raw(website=None, city=variant, source="csv")], session)
    assert (rep.created, rep.updated) == (0, 1), variant
    assert _one(session).city == "Konya"


def test_unnormalized_city_already_in_db_still_matches(session):
    session.add(Company(name="Gama Teknoloji", city="ISTANBUL"))
    session.commit()
    rep = ing.ingest([_raw(name="Gama Teknoloji A.Ş.", website=None, city="İstanbul")], session)
    assert (rep.created, rep.updated) == (0, 1)


def test_cityless_company_gets_city_on_second_pass_without_duplicate(session):
    ing.ingest([_raw(name="Zeta", website=None, city=None)], session, allow_non_target=True)
    rep = ing.ingest([_raw(name="Zeta Ltd.", website=None, city="Konya", source="csv")], session)
    assert (rep.created, rep.updated) == (0, 1)
    assert _one(session).city == "Konya"


def test_name_only_fallback_never_merges_two_known_cities(session):
    ing.ingest([_raw(name="Gama", website=None, city="Konya"), _raw(name="Gama", website=None, city="Ankara")], session)
    rep = ing.ingest([_raw(name="Gama", website=None, city="İzmir")], session)
    assert rep.created == 1
    assert len(session.scalars(select(Company)).all()) == 3


def test_city_given_as_non_string_does_not_crash(session):
    rep = ing.ingest([_raw(city=42)], session)  # type: ignore[arg-type]
    assert rep.errors == [] and rep.skipped_city == 1


# --- hata toleransı / savepoint -----------------------------------------------------------------

def test_failure_after_company_flush_rolls_back_only_that_record(session, monkeypatch):
    orig = ing._apply_contacts
    calls = {"n": 0}

    def boom(s, company, raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kişi patladı")
        return orig(s, company, raw)

    monkeypatch.setattr(ing, "_apply_contacts", boom)
    rep = ing.ingest(
        [
            _raw(name="Kötü", website="https://kotu.com", emails=["a@kotu.com"]),
            _raw(name="İyi", website="https://iyi.com", emails=["a@iyi.com"]),
        ],
        session,
    )
    session.commit()
    assert (rep.created, len(rep.errors)) == (1, 1) and "Kötü" in rep.errors[0]
    assert [c.name for c in session.scalars(select(Company))] == ["İyi"]
    assert [k.email for k in session.scalars(select(Contact))] == ["a@iyi.com"]
    assert len(session.scalars(select(Event)).all()) == 1
    assert len(session.scalars(select(CompanySource)).all()) == 1


def test_integrity_error_in_one_record_does_not_abort_batch(session):
    raws = [_raw(), _raw(name="Kaynaksız", website="https://kaynaksiz.com", source=None), _raw(name="Beta", website="https://beta.com")]  # type: ignore[arg-type]
    rep = ing.ingest(raws, session)
    assert rep.created == 2 and len(rep.errors) == 1
    assert sorted(c.name for c in session.scalars(select(Company))) == ["Alfa Yazılım A.Ş.", "Beta"]


def test_generator_raising_midway_keeps_processed_records(session):
    def gen():
        yield _raw()
        yield _raw(name="Beta", website="https://beta.com")
        raise RuntimeError("collector patladı")

    rep = ing.ingest(gen(), session)
    assert rep.created == 2 and len(rep.errors) == 1 and "Kaynak akışı kesildi" in rep.errors[0]


def test_odd_raw_payloads_do_not_crash(session):
    rep = ing.ingest(
        [
            _raw(emails=[None, "", "  IK@ALFA.COM.TR ", "gecersiz@"], raw=["liste", 1], tags=None),  # type: ignore[list-item,arg-type]
            _raw(name="Beta", website="https://beta.com", raw={"notes": 5, "contact_name": 7}),
        ],
        session,
    )
    assert rep.errors == [] and rep.created == 2
    alfa = session.scalar(select(Company).where(Company.domain == "alfa.com.tr"))
    assert [k.email for k in alfa.contacts] == ["ik@alfa.com.tr"]
    assert alfa.sources[0].raw == ["liste", 1]


def test_contact_name_goes_to_personal_or_single_email_only(session):
    ing.ingest([_raw(emails=["info@alfa.com.tr", "ayse.y@alfa.com.tr"], raw={"contact_name": "Ayşe Yılmaz"})], session)
    ing.ingest([_raw(name="Beta", website="https://beta.com", emails=["info@beta.com"], raw={"contact_name": "Ali"})], session)
    names = {k.email: k.name for k in session.scalars(select(Contact))}
    assert names == {"info@alfa.com.tr": None, "ayse.y@alfa.com.tr": "Ayşe Yılmaz", "info@beta.com": "Ali"}
    assert session.scalar(select(Contact).where(Contact.email == "ayse.y@alfa.com.tr")).kind == ContactKind.personal


def test_report_total_counts_everything(session):
    rep = ing.ingest([_raw(), _raw(city="Trabzon"), _raw(name=""), "x"], session)  # type: ignore[list-item]
    assert rep.total == 4 and rep.created == 1 and rep.skipped_city == 1 and rep.skipped_invalid == 2


# --- idempotency ve performans -------------------------------------------------------------------

def test_reingest_of_hundreds_of_rows_is_idempotent_and_has_no_per_row_lazy_loads(engine):
    n = 300
    raws = [
        RawCompany(name=f"Firma {i} A.Ş.", source="argem", city="Konya", website=f"https://firma{i}.com.tr",
                   emails=[f"info@firma{i}.com.tr", f"ik@firma{i}.com.tr"], tags=["t"], is_arge_center=True)
        for i in range(n)
    ]
    Session = get_sessionmaker(engine)
    with Session() as s:
        r1 = ing.ingest(raws, s)
        s.commit()
    assert (r1.created, r1.errors) == (n, [])

    stmts: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cur, statement, params, ctx, many):  # noqa: ANN001
        stmts.append(statement.strip().split()[0].upper())

    try:
        again = [
            RawCompany(name=f"FIRMA {i} ANONİM ŞİRKETİ", source="teknopark", city="konya", website=f"http://www.firma{i}.com.tr/",
                       emails=[f"info@firma{i}.com.tr"], is_teknopark=True)
            for i in range(n)
        ]
        with Session() as s:  # taze oturum: ilişkiler bellekte değil
            r2 = ing.ingest(again, s)
            s.commit()
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert (r2.created, r2.updated, r2.errors) == (0, n, [])
    selects = stmts.count("SELECT")
    assert selects < 25, f"kayıt başına SELECT var: {selects}"

    with Session() as s:
        assert len(s.scalars(select(Company)).all()) == n
        assert len(s.scalars(select(Contact)).all()) == 2 * n
        assert len(s.scalars(select(CompanySource)).all()) == 2 * n
        assert len(s.scalars(select(Event)).all()) == n  # yeniden toplama Event eklemez
        c = s.scalar(select(Company).where(Company.domain == "firma7.com.tr"))
        assert c.is_arge_center and c.is_teknopark and c.website == "https://firma7.com.tr"


# --- csv_import -----------------------------------------------------------------------------------

def test_title_line_above_semicolon_header(tmp_path: Path):
    p = tmp_path / "okul.csv"
    p.write_text("Okul Protokol Listesi\n\nFirma;Şehir;E-posta;Adres\nAcme;Konya;a@acme.com;Sok. 1, Kat 2, No: 3\n", encoding="utf-8")
    raws = ci.read_file(p)
    assert [(r.name, r.city, r.emails, r.address) for r in raws] == [("Acme", "Konya", ["a@acme.com"], "Sok. 1, Kat 2, No: 3")]


def test_semicolon_file_whose_cells_have_many_commas(tmp_path: Path):
    p = tmp_path / "v.csv"
    lines = ["Şirket Adı;E-posta;Adres;Şehir"]
    for i in range(8):
        lines.append(f"Firma {i};a{i}@f{i}.com, b{i}@f{i}.com, c{i}@f{i}.com;Cad. {i}, Sok. {i}, Kat {i}, No {i}, Daire {i};Konya")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raws = ci.read_file(p)
    assert len(raws) == 8 and raws[3].emails == ["a3@f3.com", "b3@f3.com", "c3@f3.com"] and raws[3].city == "Konya"


def test_utf16_export_is_read(tmp_path: Path):
    p = tmp_path / "u16.csv"
    p.write_bytes("name\tcity\temail\nÇınar Ltd.\tİzmir\tik@cinar.com.tr\n".encode("utf-16"))
    raws = ci.read_file(p)
    assert [(r.name, r.city, r.emails) for r in raws] == [("Çınar Ltd.", "İzmir", ["ik@cinar.com.tr"])]


def test_cp1254_turkish_characters_decoded(tmp_path: Path):
    p = tmp_path / "tr.csv"
    p.write_bytes("Firma;Şehir\nÖzgür Yazılım Ltd. Şti.;İzmir\nĞüneş Bilişim;Eskişehir\n".encode("cp1254"))
    raws = ci.read_file(p)
    assert [(r.name, r.city) for r in raws] == [("Özgür Yazılım Ltd. Şti.", "İzmir"), ("Ğüneş Bilişim", "Eskişehir")]


def test_header_variants_quotes_spaces_and_numbered_duplicates(tmp_path: Path):
    p = tmp_path / "h.csv"
    p.write_text(
        '﻿"Şirket Ünvanı" , "E-posta Adresi" , "E-posta 2", "Web Adresi", "İl", "Yetkili Kişi", "Telefon No*"\n'
        'Acme , a@acme.com , b@acme.com , "www.acme.com.tr, www.acme.com" , Konya , Ayşe Yılmaz , 0332\n',
        encoding="utf-8",
    )
    raws = ci.read_file(p)
    assert len(raws) == 1
    r = raws[0]
    assert r.name == "Acme" and r.emails == ["a@acme.com", "b@acme.com"] and r.website == "https://www.acme.com.tr"
    assert r.city == "Konya" and r.raw["contact_name"] == "Ayşe Yılmaz" and r.phone == "0332"


def test_header_alias_table_handles_common_turkish_forms():
    m = ci.map_headers(["Firma Ünvanı", "e-posta", "E-Mail", "İnternet Sitesi", "İlçe", "Sektör", "Açıklama", "Notlar", "Etiketler"])
    assert m == {0: "name", 1: "email", 2: "email", 3: "website", 4: "district", 5: "sector", 6: "description", 7: "notes", 8: "tags"}


def test_whitespace_only_and_separator_only_rows_are_skipped(tmp_path: Path):
    p = tmp_path / "w.csv"
    p.write_text("name,email,city\n   ,   ,   \n,,\n\n\t\nAcme,a@acme.com,Konya\n , x@y.com , Konya\n", encoding="utf-8")
    raws = ci.read_file(p)
    assert [r.name for r in raws] == ["Acme"]


def test_duplicate_rows_inside_one_csv_merge_into_one_company(session, tmp_path: Path):
    p = tmp_path / "d.csv"
    p.write_text(
        "name,website,email,city\n"
        "Acme Yazılım A.Ş.,https://www.acme.com.tr,info@acme.com.tr,Konya\n"
        "ACME YAZILIM ANONİM ŞİRKETİ,acme.com.tr/,ik@acme.com.tr,konya\n"
        "Acme Yazılım,,,Konya\n",
        encoding="utf-8",
    )
    raws = ci.read_file(p)
    assert len(raws) == 3
    rep = ing.ingest(raws, session)
    assert (rep.created, rep.updated, rep.errors) == (1, 2, [])
    c = _one(session)
    assert sorted(k.email for k in c.contacts) == ["ik@acme.com.tr", "info@acme.com.tr"]
    assert c.primary_contact.email == "ik@acme.com.tr"
    assert [s.source for s in c.sources] == ["csv"]


def test_email_cell_variants(tmp_path: Path):
    assert ci.split_emails("info@x.com., <IK@X.com>; mailto:staj@x.com, (a@b.co)") == ["info@x.com", "ik@x.com", "staj@x.com", "a@b.co"]
    assert ci.split_emails("Ayşe Yılmaz <ayse@x.com>") == ["ayse@x.com"]
    assert ci.split_emails("yok") == [] and ci.split_emails(None) == []


def test_website_cell_variants():
    assert ci.first_website("www.x.com.tr, www.y.com") == "https://www.x.com.tr"
    assert ci.first_website("x.com.tr/") == "https://x.com.tr/"
    assert ci.first_website("yok") is None and ci.first_website("-") is None and ci.first_website(None) is None
    assert ci.first_website("mailto:a@b.com www.c.com") == "https://www.c.com"


def test_xlsx_numeric_and_empty_cells(tmp_path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Firma", "Telefon", "Şehir", "E-posta", "Etiketler"])
    ws.append(["Acme", 3320000000.0, "Konya", "a@acme.com", True])
    ws.append(["Beta", 5321234567, "Konya", None, None])
    ws.append([None, None, None, None, None])
    ws.append(["   ", "", "", "", ""])
    p = tmp_path / "n.xlsx"
    wb.save(p)
    raws = ci.read_file(p)
    assert [(r.name, r.phone, r.emails) for r in raws] == [("Acme", "3320000000", ["a@acme.com"]), ("Beta", "5321234567", [])]


def test_legacy_xls_gives_turkish_error(tmp_path: Path):
    p = tmp_path / "eski.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 64)
    with pytest.raises(ValueError, match="xlsx"):
        ci.read_file(p)


def test_default_city_only_fills_blank_and_school_tag_not_duplicated(tmp_path: Path):
    p = tmp_path / "s.csv"
    p.write_text("name,city,tags\nA,Ankara,okul-protokol\nB,,x\n", encoding="utf-8")
    raws = ci.read_file(p, source="school", default_city="Konya")
    assert [(r.city, r.tags, r.school_protocol) for r in raws] == [("Ankara", ["okul-protokol"], True), ("Konya", ["x", "okul-protokol"], True)]


def test_csv_header_only_file_returns_empty(tmp_path: Path):
    p = tmp_path / "ho.csv"
    p.write_text("name,website\n", encoding="utf-8")
    assert ci.read_file(p) == []


def test_no_print_in_csv_import():
    import inspect

    assert "print(" not in inspect.getsource(ci)
