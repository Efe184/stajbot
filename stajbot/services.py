"""Üst düzey işlemler: durum değişikliği, not, onay, kişi yönetimi (paylaşılan sözleşme, MIMARI.md bölüm 9).

Tüm durum değişiklikleri buradan geçer ve zaman çizelgesine Event yazar.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .enums import (
    APP_STATUS_LABELS,
    CONTACT_KIND_PRIORITY,
    Actor,
    AppStatus,
    ContactKind,
    EventType,
    MatchMethod,
    ReviewStatus,
    SYSTEM_TRANSITIONS,
)
from .log import get_logger
from .models import Company, Contact, EmailMessage, Event
from .utils import is_valid_email, now_utc

log = get_logger(__name__)


# --- durum -----------------------------------------------------------------

def is_system_transition_allowed(old: AppStatus, new: AppStatus) -> bool:
    return new in SYSTEM_TRANSITIONS.get(old, set())


def change_status(
    session: Session,
    company: Company,
    new_status: AppStatus,
    *,
    actor: Actor = Actor.user,
    title: str | None = None,
    body: str | None = None,
    event_type: EventType = EventType.status_change,
    email_message: EmailMessage | None = None,
    occurred_at: datetime | None = None,
) -> Event:
    """Durumu değiştirir ve Event yazar. Sistem aktörü yalnızca izinli geçişleri yapabilir."""
    old = company.status
    if actor == Actor.system and old != new_status and not is_system_transition_allowed(old, new_status):
        raise ValueError(f"Sistem geçişi izinli değil: {old.value} -> {new_status.value}")
    company.status = new_status
    company.updated_at = now_utc()
    ev = Event(
        company=company,
        type=event_type,
        old_status=old,
        new_status=new_status,
        title=title or f"Durum: {APP_STATUS_LABELS[old]} → {APP_STATUS_LABELS[new_status]}",
        body=body,
        created_by=actor,
        occurred_at=occurred_at or now_utc(),
        email_message=email_message,
    )
    session.add(ev)
    session.flush()
    log.info("Durum değişti [%s] %s -> %s (%s)", company.id, old.value, new_status.value, actor.value)
    return ev


def add_event(
    session: Session,
    company: Company,
    type: EventType,
    title: str,
    *,
    body: str | None = None,
    actor: Actor = Actor.system,
    email_message: EmailMessage | None = None,
    occurred_at: datetime | None = None,
) -> Event:
    ev = Event(
        company=company,
        type=type,
        title=title,
        body=body,
        created_by=actor,
        email_message=email_message,
        occurred_at=occurred_at or now_utc(),
    )
    session.add(ev)
    session.flush()
    return ev


def add_note(session: Session, company: Company, body: str, actor: Actor = Actor.user) -> Event:
    return add_event(session, company, EventType.note, "Not", body=body, actor=actor)


# --- inceleme --------------------------------------------------------------

def approve_company(session: Session, company: Company) -> None:
    """Onayla; birincil e-posta varsa status found -> ready."""
    company.review_status = ReviewStatus.approved
    add_event(session, company, EventType.manual, "Onaylandı", actor=Actor.user)
    if company.status == AppStatus.found and company.primary_contact is not None:
        change_status(session, company, AppStatus.ready, actor=Actor.system, title="Hazır: onaylandı ve e-posta mevcut")


def reject_company(session: Session, company: Company, reason: str | None = None) -> None:
    company.review_status = ReviewStatus.rejected
    add_event(session, company, EventType.manual, "Reddedildi", body=reason, actor=Actor.user)
    if company.status in (AppStatus.found, AppStatus.ready, AppStatus.drafted):
        change_status(session, company, AppStatus.closed, actor=Actor.user, title="Kapandı: inceleme sırasında reddedildi")


def defer_company(session: Session, company: Company) -> None:
    company.review_status = ReviewStatus.later
    add_event(session, company, EventType.manual, "Sonraya bırakıldı", actor=Actor.user)


def set_blacklist(session: Session, company: Company, flag: bool) -> None:
    company.blacklisted = flag
    add_event(session, company, EventType.manual, "Kara listeye alındı" if flag else "Kara listeden çıkarıldı", actor=Actor.user)
    if flag and company.status not in (AppStatus.accepted,):
        change_status(session, company, AppStatus.closed, actor=Actor.user, title="Kapandı: kara liste")


def maybe_mark_ready(session: Session, company: Company) -> bool:
    """Onaylı + e-posta var + status found ise ready yapar."""
    if (
        company.review_status == ReviewStatus.approved
        and company.status == AppStatus.found
        and company.primary_contact is not None
    ):
        change_status(session, company, AppStatus.ready, actor=Actor.system, title="Hazır: e-posta bulundu")
        return True
    return False


# --- kişiler ---------------------------------------------------------------

def classify_contact_kind(email: str) -> ContactKind:
    local = email.split("@", 1)[0].lower()
    hr_keys = ("kariyer", "career", "ik", "hr", "insankaynaklari", "insan.kaynaklari", "staj", "intern", "basvuru", "jobs", "job", "recruit")
    info_keys = ("info", "iletisim", "contact", "bilgi", "destek", "support", "sales", "satis", "muhasebe", "noreply", "no-reply")
    if any(local == k or local.startswith(k) for k in hr_keys):
        return ContactKind.hr
    if any(local == k or local.startswith(k) for k in info_keys):
        return ContactKind.info
    if "." in local or "_" in local:
        return ContactKind.personal
    return ContactKind.other


def add_contact(
    session: Session,
    company: Company,
    email: str,
    *,
    name: str | None = None,
    role: str | None = None,
    kind: ContactKind | None = None,
    source: str = "manual",
    make_primary: bool | None = None,
) -> Contact | None:
    """Kişi ekler (varsa mevcut olanı döner). Geçersiz e-posta -> None.

    make_primary None ise: ilk kişi ya da daha iyi priority'li kişi otomatik birincil olur.
    """
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        return None
    existing = next((c for c in company.contacts if c.email == email), None)
    if existing is not None:
        if name and not existing.name:
            existing.name = name
        if role and not existing.role:
            existing.role = role
        return existing
    kind = kind or classify_contact_kind(email)
    # Mevcut birincil kişi, yeni kişi listeye eklenmeden ÖNCE belirlenir (primary_contact ilk kişiye düşer).
    current = company.primary_contact
    contact = Contact(
        company=company,
        email=email,
        name=name,
        role=role,
        kind=kind,
        priority=CONTACT_KIND_PRIORITY[kind],
        source=source,
        is_primary=False,
    )
    session.add(contact)
    if make_primary is True or current is None or (make_primary is None and contact.priority < current.priority):
        for c in company.contacts:
            c.is_primary = False
        contact.is_primary = True
    session.flush()
    maybe_mark_ready(session, company)
    return contact


def set_primary_contact(session: Session, company: Company, contact: Contact) -> None:
    for c in company.contacts:
        c.is_primary = c.id == contact.id
    session.flush()
    maybe_mark_ready(session, company)


# --- takip -----------------------------------------------------------------

def mark_no_reply(session: Session, now: datetime | None = None) -> int:
    """sent + next_followup_at geçmiş -> no_reply. Değişen sayısını döner."""
    now = now or now_utc()
    stmt = select(Company).where(Company.status == AppStatus.sent, Company.next_followup_at.is_not(None), Company.next_followup_at < now)
    n = 0
    for c in session.scalars(stmt):
        change_status(session, c, AppStatus.no_reply, actor=Actor.system, title="Cevap yok: takip süresi doldu")
        n += 1
    return n


def assign_message_to_company(session: Session, msg: EmailMessage, company: Company) -> None:
    """Sahipsiz gelen mesajı elle şirkete bağlar; gerekirse durumu replied yapar."""
    msg.company = company
    msg.matched_by = MatchMethod.manual
    company.last_contact_at = msg.received_at or now_utc()
    add_event(session, company, EventType.email_received, f"Gelen mail: {msg.subject or '(konu yok)'}", body=(msg.body_text or "")[:2000], actor=Actor.user, email_message=msg)
    if company.status in (AppStatus.sent, AppStatus.no_reply, AppStatus.followup_sent):
        change_status(session, company, AppStatus.replied, actor=Actor.system, title="Cevap geldi (elle eşleştirildi)", email_message=msg)


def companies_by_status(session: Session) -> dict[AppStatus, int]:
    from sqlalchemy import func

    rows = session.execute(select(Company.status, func.count()).group_by(Company.status)).all()
    out = {s: 0 for s in AppStatus}
    for st, n in rows:
        out[st] = n
    return out
