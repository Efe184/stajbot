"""Veritabanı bağlantısı (paylaşılan sözleşme)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def make_engine(url: str | None = None) -> Engine:
    """Yeni bir engine üretir. Testler `make_engine("sqlite:///:memory:")` kullanır."""
    url = url or get_settings().db_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    from sqlalchemy.pool import StaticPool

    kwargs = {}
    if url.endswith(":memory:"):
        kwargs["poolclass"] = StaticPool
    eng = create_engine(url, connect_args=connect_args, future=True, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return eng


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def init_db(engine: Engine | None = None) -> None:
    """Tabloları oluşturur (varsa dokunmaz)."""
    Base.metadata.create_all(engine or get_engine())


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker[Session]:
    global _SessionLocal
    if engine is not None:
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def get_session(engine: Engine | None = None) -> Iterator[Session]:
    """`with get_session() as s:` — hata olursa rollback, yoksa commit."""
    s = get_sessionmaker(engine)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def set_setting(session: Session, key: str, value: str) -> None:
    from .models import AppSetting

    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def get_setting(session: Session, key: str, default: str | None = None) -> str | None:
    from .models import AppSetting

    row = session.get(AppSetting, key)
    return row.value if row is not None else default
