"""Onay kuyruğu: bekleyen şirketler puana göre; Onayla / Reddet / Sonra (HTMX)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import services
from ...enums import ReviewStatus, normalize_city
from ...models import Company
from ..deps import get_db

router = APIRouter()
PAGE_SIZE = 40


def _pending_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(Company).where(Company.review_status == ReviewStatus.pending, Company.blacklisted.is_(False))
    ) or 0


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "on", "yes", "evet")


@router.get("/inceleme")
def review_queue(request: Request, city: Optional[str] = None, only_email: Optional[str] = None, session: Session = Depends(get_db)):
    from ..app import render

    only_email = _truthy(only_email)  # "?only_email=" gibi boş değer 422 yerine "hayır" sayılır
    stmt = select(Company).where(Company.review_status == ReviewStatus.pending, Company.blacklisted.is_(False))
    if city:
        stmt = stmt.where(Company.city == (normalize_city(city) or city))
    if only_email:
        stmt = stmt.where(Company.contacts.any())
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(session.scalars(stmt.order_by(Company.score.desc(), Company.id.asc()).limit(PAGE_SIZE)))
    cities = [c for (c,) in session.execute(select(Company.city).distinct().order_by(Company.city)).all() if c]
    return render(request, "review.html", companies=rows, total=total, cities=cities, city=city or "", only_email=only_email)


def _act(session: Session, company_id: int, action: str) -> tuple[Company, str]:
    c = session.get(Company, company_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Şirket bulunamadı")
    if action == "onayla":
        services.approve_company(session, c)
        return c, "Onaylandı" + ("" if c.primary_contact else " (e-posta yok: zenginleştirme sonrası Hazır olur)")
    if action == "reddet":
        services.reject_company(session, c)
        return c, "Reddedildi"
    if action == "sonra":
        services.defer_company(session, c)
        return c, "Sonraya bırakıldı"
    raise HTTPException(status_code=400, detail="Geçersiz işlem")


@router.post("/inceleme/{company_id}/{action}")
def review_action(request: Request, company_id: int, action: str, session: Session = Depends(get_db)):
    from ..app import is_htmx, redirect, render

    c, msg = _act(session, company_id, action)
    if is_htmx(request):
        # Kart yerine "yapıldı" satırı; başlıktaki bekleyen sayacı hx-swap-oob ile güncellenir.
        return render(request, "_review_done.html", c=c, msg=msg, pending=_pending_count(session))
    return redirect("/inceleme", f"{c.name}: {msg}")
