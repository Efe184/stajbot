"""FastAPI bağımlılıkları: veritabanı oturumu."""
from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from .. import db


def get_db() -> Iterator[Session]:
    """İstek başına bir Session; hata yoksa commit, varsa rollback."""
    session = db.get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
