"""Gelen kutusu: eşleşen / sahipsiz mesajlar, elle bağlama, duygu onayı, IMAP'tan okuma."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ... import services
from ...config import get_settings
from ...enums import APP_STATUS_LABELS, Actor, AppStatus, Direction, MatchMethod, Sentiment
from ...log import get_logger
from ...models import Company, EmailMessage
from ..deps import get_db

log = get_logger(__name__)
router = APIRouter()

SENTIMENT_STATUS = {
    Sentiment.positive: AppStatus.positive,
    Sentiment.negative: AppStatus.negative,
    Sentiment.info: AppStatus.info_requested,
}


@router.get("/gelen")
def inbox_page(request: Request, tab: str = "matched", q: str = "", session: Session = Depends(get_db)):
    from ..app import render

    tab = tab if tab in ("matched", "unmatched") else "matched"
    stmt = select(EmailMessage).where(EmailMessage.direction == Direction.inbound)
    stmt = stmt.where(EmailMessage.company_id.is_(None)) if tab == "unmatched" else stmt.where(EmailMessage.company_id.is_not(None))
    rows = list(session.scalars(stmt.order_by(EmailMessage.received_at.desc().nulls_last(), EmailMessage.id.desc()).limit(200)))
    counts = {
        "matched": session.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.direction == Direction.inbound, EmailMessage.company_id.is_not(None))) or 0,
        "unmatched": session.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.direction == Direction.inbound, EmailMessage.company_id.is_(None))) or 0,
    }
    companies = []
    if tab == "unmatched":
        cstmt = select(Company).order_by(Company.name.asc())
        if q:
            cstmt = cstmt.where(or_(Company.name.ilike(f"%{q}%"), Company.domain.ilike(f"%{q}%")))
        companies = list(session.scalars(cstmt.limit(300)))
    return render(request, "inbox.html", messages=rows, tab=tab, counts=counts, companies=companies, q=q, mail_ok=get_settings().mail_configured, sentiments=list(Sentiment))


def _get_inbound(session: Session, msg_id: int) -> EmailMessage:
    m = session.get(EmailMessage, msg_id)
    if m is None or m.direction != Direction.inbound:
        raise HTTPException(status_code=404, detail="Mesaj bulunamadı")
    return m


@router.post("/gelen/{msg_id}/bagla")
def assign(msg_id: int, company_id: int = Form(...), session: Session = Depends(get_db)):
    from ..app import redirect

    m = _get_inbound(session, msg_id)
    c = session.get(Company, company_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Şirket bulunamadı")
    services.assign_message_to_company(session, m, c)
    return redirect("/gelen?tab=matched", f"Mesaj {c.name} şirketine bağlandı")


@router.post("/gelen/{msg_id}/duygu")
def set_sentiment(msg_id: int, sentiment: str = Form(...), apply_status: bool = Form(False), session: Session = Depends(get_db)):
    from ..app import redirect

    m = _get_inbound(session, msg_id)
    try:
        s = Sentiment(sentiment)
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz değer") from None
    m.sentiment = s
    msg = "Tahmin güncellendi"
    if apply_status and m.company is not None and s in SENTIMENT_STATUS:
        new = SENTIMENT_STATUS[s]
        if m.company.status != new:
            services.change_status(session, m.company, new, actor=Actor.user, title=f"Cevap sınıflandırıldı: {APP_STATUS_LABELS[new]}", email_message=m)
        msg += f"; şirket durumu: {APP_STATUS_LABELS[new]}"
    elif apply_status and m.company is None:
        msg += " (mesaj bir şirkete bağlı değil; durum güncellenmedi)"
    return redirect("/gelen?tab=matched", msg)


@router.post("/gelen/oku")
def fetch_now(session: Session = Depends(get_db)):
    from ..app import redirect
    from ...mail import inbox as mail_inbox

    if not get_settings().mail_configured:
        return redirect("/gelen", "MAIL_USER / MAIL_PASSWORD tanımlı değil (.env)")
    msgs = mail_inbox.fetch_new(session)
    matched = sum(1 for m in msgs if m.company_id is not None)
    return redirect("/gelen", f"{len(msgs)} yeni mesaj; eşleşen: {matched}, sahipsiz: {len(msgs) - matched}")
