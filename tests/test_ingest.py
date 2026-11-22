"""ingest.py testleri: tekilleştirme, boş alan doldurma, hedef şehir filtresi, hata toleransı."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from stajbot.collectors import ingest as ing
from stajbot.collectors.base import RawCompany
from stajbot.enums import AppStatus, ContactKind, EventType, ReviewStatus, SizeBucket
from stajbot.models import Company, CompanySource, Contact, Event


def _raw(**kw) -> RawCompany:
    base = dict(name="Alfa Yazılım A.Ş.", source="teknopark", city="Konya", website="https://www.alfa.com.tr")
    base.update(kw)
    return RawCompany(**base)


def test_creates_company_with_source_contact_event(session):
    rep = ing.ingest([_raw(emails=["kariyer@alfa.com.tr", "info@alfa.com.tr"], tags=["x"], is_teknopark=True, teknopark_name="Konya Teknokent")], session)
    assert (rep.created, rep.updated, rep.skipped_city, rep.skipped_invalid, rep.errors) == (1, 0, 0, 0, [])
    c = session.scalars(select(Company)).one()
    assert c.domain == "alfa.com.tr" and c.city == "Konya" and c.status == AppStatus.found
    assert c.review_status == ReviewStatus.pending and c.is_teknopark and c.teknopark_name == "Konya Teknokent"
    assert c.tags == ["x"]
    assert [s.source for s in c.sources] == ["teknopark"]
    assert {k.email: k.kind for k in c.contacts} == {"kariyer@alfa.com.tr": ContactKind.hr, "info@alfa.com.tr": ContactKind.info}
    assert c.primary_contact.email == "kariyer@alfa.com.tr"
    assert [e.type for e in c.events] == [EventType.collect]


def test_second_run_is_idempotent(session):
    raws = [_raw(), _raw(name="Beta Bilişim", website=None, city="Ankara")]
    r1 = ing.ingest(raws, session)
    session.commit()
    r2 = ing.ingest(raws, session)
    session.commit()
    assert (r1.created, r2.created, r2.updated) == (2, 0, 2)
    assert session.scalar(select(Company).where(Company.name == "Beta Bilişim")) is not None
    assert len(session.scalars(select(Company)).all()) == 2
    assert len(session.scalars(select(CompanySource)).all()) == 2
    # yeniden toplama Event eklemez
    assert len(session.scalars(select(Event)).all()) == 2


def test_matches_by_domain_across_sources_and_fills_blanks(session):
    ing.ingest([_raw(source="argem", website="http://alfa.com.tr/", city="Konya", is_arge_center=True)], session)
    rep = ing.ingest([_raw(source="google_places", website="https://www.alfa.com.tr/tr", city="Konya", phone="0332 000", address="Selçuklu", emails=["info@alfa.com.tr"], is_teknopark=True)], session)
    assert (rep.created, rep.updated) == (0, 1)
    c = session.scalars(select(Company)).one()
    assert c.is_arge_center and c.is_teknopark
    assert c.phone == "0332 000" and c.address == "Selçuklu"
    assert c.website == "http://alfa.com.tr/"  # dolu alan üzerine yazılmaz
    assert sorted(c.source_names) == ["argem", "google_places"]
    assert [k.email for k in c.contacts] == ["info@alfa.com.tr"]


def test_matches_by_normalized_name_and_city_without_website(session):
    ing.ingest([_raw(name="Bosch Sanayi ve Ticaret A.Ş.", website=None, city="Bursa", source="argem")], session)
    rep = ing.ingest([_raw(name="BOSCH SANAYİ VE TİCARET ANONİM ŞİRKETİ", website="https://bosch.com.tr", city="bursa", source="teknopark")], session)
    assert (rep.created, rep.updated) == (0, 1)
    c = session.scalars(select(Company)).one()
    assert c.domain == "bosch.com.tr" and c.website == "https://bosch.com.tr"
    assert c.name == "Bosch Sanayi ve Ticaret A.Ş."


def test_same_name_different_city_are_different_companies(session):
    rep = ing.ingest([_raw(name="Gama Teknoloji", website=None, city="Konya"), _raw(name="Gama Teknoloji", website=None, city="Ankara")], session)
    assert rep.created == 2


def test_non_target_city_skipped_unless_allowed(session):
    rep = ing.ingest([_raw(city="Trabzon"), _raw(city=None, website=None, name="Şehirsiz")], session)
    assert rep.skipped_city == 2 and rep.created == 0
    rep2 = ing.ingest([_raw(city="Trabzon")], session, allow_non_target=True)
    assert rep2.created == 1
    assert session.scalars(select(Company)).one().city == "Trabzon"


def test_city_aliases_normalized(session):
    ing.ingest([_raw(city="ISTANBUL / Kadıköy"), _raw(name="B", website="https://b.com", city="afyon")], session)
    cities = sorted(c.city for c in session.scalars(select(Company)))
    assert cities == ["Afyonkarahisar", "İstanbul"]


def test_invalid_rows(session):
    rep = ing.ingest([_raw(name=""), _raw(name="   "), "not a raw"], session)  # type: ignore[list-item]
    assert rep.skipped_invalid == 3 and rep.created == 0


def test_generic_email_domain_is_not_company_domain(session):
    rep = ing.ingest([_raw(website=None, emails=["ahmet@gmail.com"]), _raw(name="Başka Firma", website=None, emails=["ayse@gmail.com"])], session)
    assert rep.created == 2
    assert all(c.domain is None for c in session.scalars(select(Company)))
    assert len(session.scalars(select(Contact)).all()) == 2


def test_social_domain_is_not_domain_key(session):
    ing.ingest([_raw(website="https://www.linkedin.com/company/alfa"), _raw(name="Beta", website="https://linkedin.com/company/beta")], session)
    assert all(c.domain is None for c in session.scalars(select(Company)))
    assert len(session.scalars(select(Company)).all()) == 2


def test_email_domain_used_when_no_website(session):
    ing.ingest([_raw(website=None, emails=["ik@alfa.com.tr"])], session)
    rep = ing.ingest([_raw(name="Alfa Yazilim Teknoloji Ltd.", website="https://www.alfa.com.tr")], session)
    assert rep.updated == 1 and rep.created == 0
    c = session.scalars(select(Company)).one()
    assert c.website == "https://www.alfa.com.tr"


def test_raw_json_made_safe(session):
    ing.ingest([_raw(raw={"tarih": date(2024, 1, 2), "liste": {1, 2}})], session)
    src = session.scalars(select(CompanySource)).one()
    assert src.raw["tarih"] == "2024-01-02"


def test_employee_count_sets_bucket_and_notes(session):
    ing.ingest([_raw(employee_count=120, raw={"notes": "okuldan geldi", "contact_name": "Ayşe"}, emails=["ayse.y@alfa.com.tr"])], session)
    c = session.scalars(select(Company)).one()
    assert c.size_bucket == SizeBucket.s51_200 and c.notes == "okuldan geldi"
    assert c.contacts[0].name == "Ayşe" and c.contacts[0].kind == ContactKind.personal


def test_tags_merge_and_flags_or(session):
    ing.ingest([_raw(tags=["a"], school_protocol=True)], session)
    ing.ingest([_raw(source="csv", tags=["a", "b"], is_arge_center=True)], session)
    c = session.scalars(select(Company)).one()
    assert c.tags == ["a", "b"] and c.school_protocol and c.is_arge_center


def test_duplicates_within_same_batch(session):
    rep = ing.ingest([_raw(), _raw(name="ALFA YAZILIM LTD. ŞTİ.", website=None), _raw(website="http://alfa.com.tr")], session)
    assert (rep.created, rep.updated) == (1, 2)


def test_domain_conflict_does_not_crash(session):
    ing.ingest([_raw(name="Alfa", website="https://alfa.com.tr", city="Konya")], session)
    ing.ingest([_raw(name="Alfa Grup", website=None, city="Ankara")], session)
    # 'Alfa Grup' (Ankara) için ad+şehir eşleşmesi; alfa.com.tr başka şirkete ait -> domain atanmaz, hata yok
    rep = ing.ingest([_raw(name="Alfa Grup", website="https://alfa.com.tr", city="Ankara")], session)
    assert rep.errors == [] and rep.updated == 1
    rows = session.scalars(select(Company)).all()
    assert len(rows) == 2


def test_error_in_one_record_does_not_abort(session, monkeypatch):
    calls = {"n": 0}
    orig = ing._create

    def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("patladı")
        return orig(*a, **kw)

    monkeypatch.setattr(ing, "_create", boom)
    rep = ing.ingest([_raw(name="Kötü"), _raw(name="İyi", website="https://iyi.com")], session)
    assert rep.created == 1 and len(rep.errors) == 1 and "Kötü" in rep.errors[0]
    assert [c.name for c in session.scalars(select(Company))] == ["İyi"]


def test_name_truncated_and_report_summary(session):
    rep = ing.ingest([_raw(name="A" * 300, website=None)], session)
    assert session.scalars(select(Company)).one().name.__len__() <= 255
    assert "yeni: 1" in rep.summary()


def test_no_print_in_module():
    import inspect

    src = inspect.getsource(ing)
    assert "print(" not in src
