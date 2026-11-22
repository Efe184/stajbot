"""Ortak test fikstürleri: bellek içi veritabanı ve örnek şirket."""
from __future__ import annotations

import pytest

from stajbot.db import get_sessionmaker, init_db, make_engine
from stajbot.enums import ReviewStatus
from stajbot.models import Company
from stajbot import services


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Testler kullanıcının .env'indeki gerçek Gmail / API bilgileriyle asla dışarı bağlanmasın."""
    from stajbot.config import get_settings

    s = get_settings()
    for attr in ("mail_user", "mail_password", "anthropic_api_key", "google_places_api_key"):
        monkeypatch.setattr(s, attr, "")
    monkeypatch.setattr(s, "mail_mode", "dry")


@pytest.fixture()
def engine():
    eng = make_engine("sqlite:///:memory:")
    init_db(eng)
    return eng


@pytest.fixture()
def session(engine):
    Session = get_sessionmaker(engine)
    s = Session()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def company(session):
    """Onaylı, HR e-postası olan, İstanbul'da bir örnek şirket (status: ready)."""
    c = Company(
        name="Örnek Yazılım A.Ş.",
        domain="ornekyazilim.com",
        website="https://www.ornekyazilim.com",
        city="İstanbul",
        sector="Fintech",
        tech_stack=["Python", "React", "AWS"],
        description="Ödeme sistemleri geliştiren bir fintech şirketi.",
        review_status=ReviewStatus.approved,
    )
    session.add(c)
    session.flush()
    services.add_contact(session, c, "kariyer@ornekyazilim.com", source="test")
    session.commit()
    session.refresh(c)
    return c


@pytest.fixture()
def fixtures_dir():
    from pathlib import Path

    return Path(__file__).parent / "fixtures"
