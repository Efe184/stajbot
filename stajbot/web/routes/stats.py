"""Pano (/) ve istatistik (/istatistik) sayfaları."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import services
from ...enums import AppStatus, Direction, MailStatus, ReviewStatus
from ...models import Company, CompanySource, EmailMessage, Event
from ...utils import now_utc
from ..deps import get_db

router = APIRouter()

SENT_STATUSES = {
    AppStatus.sent, AppStatus.no_reply, AppStatus.followup_sent, AppStatus.replied, AppStatus.positive,
    AppStatus.negative, AppStatus.info_requested, AppStatus.phone_call, AppStatus.interview,
    AppStatus.presentation, AppStatus.offer, AppStatus.accepted,
}
REPLIED_STATUSES = {
    AppStatus.replied, AppStatus.positive, AppStatus.negative, AppStatus.info_requested, AppStatus.phone_call,
    AppStatus.interview, AppStatus.presentation, AppStatus.offer, AppStatus.accepted,
}
POSITIVE_STATUSES = {AppStatus.positive, AppStatus.phone_call, AppStatus.interview, AppStatus.presentation, AppStatus.offer, AppStatus.accepted}


def _count(session: Session, stmt) -> int:
    return session.scalar(stmt) or 0


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_db)):
    from ..app import render

    now = now_utc()
    today = datetime(now.year, now.month, now.day)
    by_status = services.companies_by_status(session)
    total = sum(by_status.values())
    pending_review = _count(session, select(func.count()).select_from(Company).where(Company.review_status == ReviewStatus.pending))
    sent_today = _count(
        session,
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.direction == Direction.outbound, EmailMessage.status == MailStatus.sent, EmailMessage.sent_at >= today
        ),
    )
    drafts = _count(session, select(func.count()).select_from(EmailMessage).where(EmailMessage.status == MailStatus.draft))
    approved = _count(session, select(func.count()).select_from(EmailMessage).where(EmailMessage.status == MailStatus.approved))
    # /gelen "Sahipsiz" sekmesiyle aynı ölçüt: şirkete bağlı olmayan gelen mesajlar.
    unmatched = _count(
        session,
        select(func.count()).select_from(EmailMessage).where(EmailMessage.direction == Direction.inbound, EmailMessage.company_id.is_(None)),
    )
    upcoming = list(
        session.scalars(
            select(Company)
            .where(Company.status == AppStatus.sent, Company.next_followup_at.is_not(None), Company.next_followup_at <= now + timedelta(days=3))
            .order_by(Company.next_followup_at.asc())
            .limit(10)
        )
    )
    recent_events = list(session.scalars(select(Event).order_by(Event.occurred_at.desc()).limit(12)))
    sent_like = sum(by_status.get(s, 0) for s in SENT_STATUSES)
    replied_like = sum(by_status.get(s, 0) for s in REPLIED_STATUSES)
    return render(
        request, "index.html",
        total=total, by_status=by_status, pending_review=pending_review, sent_today=sent_today, drafts=drafts,
        approved=approved, unmatched=unmatched, upcoming=upcoming, recent_events=recent_events,
        sent_like=sent_like, replied_like=replied_like,
        reply_rate=(100 * replied_like / sent_like) if sent_like else None,
    )


def _breakdown(rows) -> list[dict]:
    """(anahtar, status, n) satırlarını toplam / gönderilen / cevap / olumlu tablosuna çevirir."""
    agg: dict[str, dict] = defaultdict(lambda: {"total": 0, "sent": 0, "replied": 0, "positive": 0})
    for key, status, n in rows:
        k = key or "(bilinmiyor)"
        agg[k]["total"] += n
        if status in SENT_STATUSES:
            agg[k]["sent"] += n
        if status in REPLIED_STATUSES:
            agg[k]["replied"] += n
        if status in POSITIVE_STATUSES:
            agg[k]["positive"] += n
    out = []
    for k, v in agg.items():
        v["key"] = k
        v["reply_rate"] = (100 * v["replied"] / v["sent"]) if v["sent"] else None
        out.append(v)
    out.sort(key=lambda r: (-r["total"], r["key"]))
    return out


@router.get("/istatistik")
def statistics(request: Request, session: Session = Depends(get_db)):
    from ..app import render

    by_source = _breakdown(
        session.execute(
            select(CompanySource.source, Company.status, func.count(func.distinct(Company.id)))
            .join(Company, Company.id == CompanySource.company_id)
            .group_by(CompanySource.source, Company.status)
        ).all()
    )
    by_city = _breakdown(session.execute(select(Company.city, Company.status, func.count()).group_by(Company.city, Company.status)).all())
    by_sector = _breakdown(session.execute(select(Company.sector, Company.status, func.count()).group_by(Company.sector, Company.status)).all())
    by_status = services.companies_by_status(session)
    mail_rows = session.execute(select(EmailMessage.status, func.count()).group_by(EmailMessage.status)).all()
    mails = {m: 0 for m in MailStatus}
    for st, n in mail_rows:
        mails[st] = n
    return render(request, "stats.html", by_source=by_source, by_city=by_city, by_sector=by_sector[:30], by_status=by_status, mails=mails)
