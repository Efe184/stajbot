"""Onaylı taslakları gönderir: SMTP / Gmail taslağı / prova. Bkz. MIMARI.md bölüm 8.

Kurallar: yalnızca ``MailStatus.approved``; günlük limit (yerel takvim gününe göre, bugün ``sent`` olan
mesajlar); gönderimler arası rastgele bekleme; mesai penceresi (``force`` ile aşılır); kara liste ve
``NO_MAIL_STATUSES`` şirketlere asla; aynı şirkete ``min_days_between_mails`` içinde ikinci başvuru maili yok
(hatırlatma şablonu bu kuraldan muaftır, çünkü kullanıcı onaylamıştır ve followup_days < min_days olabilir).

``MAIL_MODE``: ``send`` SMTP ile gönderir; ``draft`` Gmail'in Taslaklar klasörüne koyar (mesaj veritabanında
yine 'sent' sayılır, kullanıcı Gmail'den gönderir); ``dry`` yalnızca loglar (veritabanı değişmez).

Her mesajdan sonra ``session.commit()`` yapılır: gönderimler arası bekleme dakikalar sürdüğü için yarıda kesilen
bir çalıştırma (Ctrl+C, süreç ölmesi) gönderilmiş mailleri 'approved' bırakıp bir sonraki çalıştırmada
yeniden göndermemelidir.
"""
from __future__ import annotations

import imaplib
import mimetypes
import random
import re
import smtplib
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import services
from ..config import Settings, get_settings
from ..enums import NO_MAIL_STATUSES, Actor, AppStatus, Direction, EventType, MailStatus
from ..log import get_logger
from ..models import Company, EmailMessage
from ..utils import domain_of, is_valid_email, now_utc

log = get_logger(__name__)

GMAIL_DRAFTS = "[Gmail]/Drafts"

# Başvuru maili gönderilince şirketin otomatik durum zinciri (sistem geçişleri).
_STATUS_CHAIN: dict[AppStatus, tuple[AppStatus, str]] = {
    AppStatus.ready: (AppStatus.drafted, "Taslak üretildi"),
    AppStatus.drafted: (AppStatus.queued, "Gönderim kuyruğuna alındı"),
    AppStatus.queued: (AppStatus.sent, "Mail gönderildi"),
}

# Bu hatalar tüm çalıştırmayı durdurur (sonraki mailler de düşerdi): kimlik / bağlantı sorunları.
_AUTH_ERRORS: tuple[type[BaseException], ...] = (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error)
_CONNECT_ERRORS: tuple[type[BaseException], ...] = (smtplib.SMTPConnectError, ConnectionRefusedError, socket.gaierror)

_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+?)\s*$')


@dataclass
class SendReport:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    mode: str = "dry"
    dry_run: bool = False
    reason: str | None = None
    sent_ids: list[int] = field(default_factory=list)

    def add_reason(self, text: str) -> None:
        self.reason = f"{self.reason}; {text}" if self.reason else text

    def summary(self) -> str:
        head = "Prova" if self.dry_run else "Gönderim"
        s = f"{head} — mod: {self.mode}; gönderilen: {self.sent}, atlanan: {self.skipped}, hatalı: {self.failed}"
        if self.reason:
            s += f" ({self.reason})"
        return s


# --- zaman / limit --------------------------------------------------------------

def within_send_window(now: datetime | None = None, settings: Settings | None = None) -> bool:
    """Yerel saat mesai penceresinde mi (ve gerekiyorsa hafta içi mi)? Başlangıç ve bitiş dahildir."""
    s = settings or get_settings()
    now = now or datetime.now()
    if s.send_weekdays_only and now.weekday() >= 5:
        return False
    start, end = s.send_window_times
    return start <= now.time() <= end


def local_day_start_utc(now: datetime, tz: tzinfo | None = None) -> datetime:
    """``now`` (saat dilimsiz UTC) anının içinde bulunduğu *yerel* takvim gününün başlangıcını UTC (saat dilimsiz)
    olarak döner. ``tz`` verilmezse sistemin yerel saat dilimi kullanılır."""
    aware = now.replace(tzinfo=timezone.utc)
    local = aware.astimezone(tz) if tz is not None else aware.astimezone()
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def sent_today(session: Session, now: datetime | None = None, *, tz: tzinfo | None = None) -> int:
    """Bugün (yerel takvim günü) gönderilmiş sayılan dış mesaj sayısı; günlük limit buna göre hesaplanır."""
    now = now or now_utc()
    day_start = local_day_start_utc(now, tz)
    return session.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.direction == Direction.outbound,
            EmailMessage.status == MailStatus.sent,
            EmailMessage.sent_at.is_not(None),
            EmailMessage.sent_at >= day_start,
        )
    ) or 0


def _recent_mail_block(company: Company, now: datetime, settings: Settings) -> str | None:
    cutoff = now - timedelta(days=settings.min_days_between_mails)
    recent = [
        m.sent_at for m in company.emails
        if m.direction == Direction.outbound and m.status == MailStatus.sent and m.sent_at and m.sent_at > cutoff
    ]
    if recent:
        return f"son {settings.min_days_between_mails} gün içinde mail gönderilmiş ({max(recent):%d.%m.%Y})"
    return None


def eligibility(msg: EmailMessage, now: datetime, settings: Settings, *, in_run: set[int]) -> str | None:
    """Mesaj gönderilebilir mi? Engel varsa Türkçe neden, yoksa None."""
    c = msg.company
    if c is None:
        return "şirket yok"
    if c.blacklisted:
        return "şirket kara listede"
    if c.status in NO_MAIL_STATUSES:
        return f"şirket durumu '{c.status.value}'"
    if not msg.to_addr:
        return "alıcı adresi yok"
    if not is_valid_email(msg.to_addr):
        return f"alıcı adresi geçersiz ({msg.to_addr})"
    if c.id in in_run:
        return "bu çalıştırmada aynı şirkete zaten gönderildi"
    if (msg.template_name or "") != "followup":
        block = _recent_mail_block(c, now, settings)
        if block:
            return block
    return None


# --- MIME -----------------------------------------------------------------------

def new_message_id(msg: EmailMessage, settings: Settings) -> str:
    dom = domain_of(settings.mail_user) or "stajbot.local"
    return make_msgid(idstring=(msg.tracking_code or "bv").lower(), domain=dom)


def build_mime(msg: EmailMessage, settings: Settings | None = None, *, message_id: str | None = None, attachments: bool = True) -> MimeMessage:
    s = settings or get_settings()
    mime = MimeMessage()
    from_name = s.mail_from_name or s.user_full_name
    mime["From"] = formataddr((from_name, s.mail_user)) if s.mail_user else from_name
    mime["To"] = msg.to_addr or ""
    if msg.cc:
        mime["Cc"] = msg.cc
    mime["Subject"] = msg.subject or ""
    mime["Date"] = formatdate(localtime=True)
    mime["Message-ID"] = message_id or msg.message_id or new_message_id(msg, s)
    if msg.in_reply_to:
        mime["In-Reply-To"] = msg.in_reply_to
    if msg.references_header:
        mime["References"] = msg.references_header
    mime.set_content(msg.body_text or "", subtype="plain", charset="utf-8")
    if msg.body_html:
        mime.add_alternative(msg.body_html, subtype="html", charset="utf-8")
    if attachments:
        seen: set[str] = set()
        for p in s.abs_attachments:
            key = str(p.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                data = p.read_bytes()
            except OSError as e:
                log.warning("Ek okunamadı %s: %s", p, e)
                continue
            ctype = "application/pdf" if p.suffix.lower() == ".pdf" else (mimetypes.guess_type(p.name)[0] or "application/octet-stream")
            maintype, subtype = ctype.split("/", 1)
            mime.add_attachment(data, maintype=maintype, subtype=subtype, filename=p.name)
    return mime


# --- taşıma -----------------------------------------------------------------------

def smtp_send(mime: MimeMessage, settings: Settings, factory: Callable[..., Any] | None = None) -> None:
    """SMTP ile gönderir. 465 portunda doğrudan TLS (SMTP_SSL), diğerlerinde STARTTLS."""
    implicit_tls = settings.smtp_port == 465
    factory = factory or (smtplib.SMTP_SSL if implicit_tls else smtplib.SMTP)
    with factory(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if not implicit_tls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(settings.mail_user, settings.mail_password)
        smtp.send_message(mime)


def find_drafts_folder(imap: Any) -> str:
    """Taslaklar klasörünü bulur. Gmail klasör adlarını hesabın diline göre verir ("[Gmail]/Taslaklar" gibi);
    LIST yanıtındaki ``\\Drafts`` özniteliği dilden bağımsızdır. Bulunamazsa ``GMAIL_DRAFTS``."""
    try:
        typ, rows = imap.list()
    except Exception as e:  # noqa: BLE001 - keşif başarısızsa varsayılan ad denenir
        log.debug("IMAP LIST başarısız: %s", e)
        return GMAIL_DRAFTS
    if typ != "OK" or not rows:
        return GMAIL_DRAFTS
    for row in rows:
        if row is None:
            continue
        if isinstance(row, (bytes, bytearray)):
            row = bytes(row).decode("utf-8", "replace")
        m = _LIST_RE.match(str(row).strip())
        if not m:
            continue
        if "\\drafts" in m.group("flags").lower():
            return m.group("name").strip().strip('"')
    return GMAIL_DRAFTS


def imap_append_draft(mime: MimeMessage, settings: Settings, factory: Callable[..., Any] | None = None, folder: str | None = None) -> str:
    """Mesajı Gmail Taslaklar klasörüne (``\\Draft`` bayrağıyla) ekler; kullanılan klasör adını döner."""
    factory = factory or imaplib.IMAP4_SSL
    imap = factory(settings.imap_host, settings.imap_port)
    try:
        imap.login(settings.mail_user, settings.mail_password)
        target = folder or find_drafts_folder(imap)
        typ, data = imap.append(f'"{target}"', "\\Draft", imaplib.Time2Internaldate(time.time()), mime.as_bytes())
        if typ != "OK":
            detail = b" ".join(d for d in (data or []) if isinstance(d, (bytes, bytearray))).decode("utf-8", "replace")
            raise RuntimeError(f"IMAP APPEND başarısız ({target}): {typ} {detail}".strip())
        return target
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


# --- ana akış ---------------------------------------------------------------------

def _mark_sent(session: Session, msg: EmailMessage, message_id: str, now: datetime, settings: Settings, *, note: str) -> None:
    c = msg.company
    msg.message_id = message_id
    msg.from_addr = settings.mail_user or msg.from_addr
    msg.status = MailStatus.sent
    msg.sent_at = now
    msg.error = None
    c.last_contact_at = now
    c.next_followup_at = now + timedelta(days=settings.followup_days)
    if (msg.template_name or "") == "followup":
        if c.status == AppStatus.sent:
            services.change_status(session, c, AppStatus.no_reply, actor=Actor.system, title="Cevap yok: hatırlatma gönderiliyor", email_message=msg)
        if c.status == AppStatus.no_reply:
            services.change_status(session, c, AppStatus.followup_sent, actor=Actor.system, title="Hatırlatma gönderildi", email_message=msg)
    else:
        while c.status in _STATUS_CHAIN:
            nxt, title = _STATUS_CHAIN[c.status]
            services.change_status(session, c, nxt, actor=Actor.system, title=title, email_message=msg)
    services.add_event(session, c, EventType.email_sent, f"Mail gönderildi: {msg.subject or ''}"[:255], body=note, actor=Actor.system, email_message=msg, occurred_at=now)
    session.flush()


def _commit(session: Session, report: SendReport) -> None:
    try:
        session.commit()
    except Exception as e:  # noqa: BLE001
        log.error("Veritabanı kaydı başarısız: %s", e)
        report.errors.append(f"veritabanı kaydı başarısız: {e}")
        session.rollback()


def _mark_failed(session: Session, msg: EmailMessage, error: str, report: SendReport) -> None:
    msg.status = MailStatus.failed
    msg.error = error
    report.failed += 1
    report.errors.append(f"[{msg.company_id}] {error}")
    _commit(session, report)


def send_approved(
    session: Session,
    *,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    smtp_factory: Callable[..., Any] | None = None,
    imap_factory: Callable[..., Any] | None = None,
) -> SendReport:
    """Onaylı mesajları gönderir. Hiç exception fırlatmaz; sorunlar raporda. Ctrl+C çalıştırmayı düzgün durdurur."""
    settings = get_settings()
    now = now or now_utc()
    report = SendReport(mode=settings.mail_mode, dry_run=dry_run)
    stmt = (
        select(EmailMessage)
        .where(EmailMessage.direction == Direction.outbound, EmailMessage.status == MailStatus.approved)
        .order_by(EmailMessage.created_at.asc(), EmailMessage.id.asc())
    )
    msgs = list(session.scalars(stmt))
    if not msgs:
        report.reason = "onaylı mesaj yok"
        return report
    if not force and not within_send_window(datetime.now(), settings):
        report.skipped = len(msgs)
        report.reason = f"mesai penceresi dışında ({settings.send_window}{', hafta içi' if settings.send_weekdays_only else ''}); --force ile aşılır"
        log.info("Gönderim atlandı: %s", report.reason)
        return report
    remaining = max(0, settings.daily_send_limit - sent_today(session, now))
    if limit is not None:
        remaining = min(remaining, limit)
    if remaining <= 0:
        report.skipped = len(msgs)
        report.reason = f"günlük limit doldu ({settings.daily_send_limit})"
        log.info("Gönderim atlandı: %s", report.reason)
        return report
    simulate = dry_run or settings.mail_mode == "dry"
    if not simulate and not settings.mail_configured:
        report.skipped = len(msgs)
        report.reason = "MAIL_USER / MAIL_PASSWORD tanımlı değil"
        return report

    in_run: set[int] = set()
    todo: list[EmailMessage] = []
    cut = 0
    for m in msgs:
        why = eligibility(m, now, settings, in_run=in_run)
        if why:
            report.skipped += 1
            report.errors.append(f"[{m.company_id}] atlandı: {why}")
            log.info("Atlandı [%s]: %s", m.company_id, why)
            continue
        if len(todo) >= remaining:
            report.skipped += 1
            cut += 1
            continue
        in_run.add(m.company_id)
        todo.append(m)
    if cut:
        report.add_reason(f"limit: bu çalıştırmada en çok {remaining} mail; {cut} mesaj sonraki güne kaldı")
    if not todo:
        report.add_reason("gönderilebilir mesaj yok (tümü kurallara takıldı)")
        log.info(report.summary())
        return report

    for i, m in enumerate(todo):
        c = m.company
        mid = new_message_id(m, settings)
        if simulate:
            log.info("[PROVA] %s -> %s | %s", c.name, m.to_addr, m.subject)
            report.sent += 1
            continue
        try:
            mime = build_mime(m, settings, message_id=mid)
            if settings.mail_mode == "draft":
                folder = imap_append_draft(mime, settings, imap_factory)
                note = f"Gmail Taslaklar klasörüne eklendi ({folder}); Gmail'den gönderin."
            else:
                smtp_send(mime, settings, smtp_factory)
                note = f"SMTP ile gönderildi ({settings.smtp_host})."
            _mark_sent(session, m, mid, now_utc(), settings, note=note)
            _commit(session, report)
            report.sent += 1
            report.sent_ids.append(m.id)
            log.info("Gönderildi [%s] %s -> %s", c.id, c.name, m.to_addr)
        except KeyboardInterrupt:
            _mark_failed(session, m, "Gönderim kullanıcı tarafından kesildi (Ctrl+C); mail gitmiş olabilir, Gmail'den kontrol edin.", report)
            report.skipped += len(todo) - i - 1
            report.add_reason("kullanıcı durdurdu (Ctrl+C); kalan mailler onaylı olarak bekliyor")
            log.warning("Gönderim kullanıcı tarafından durduruldu [%s]", c.id)
            break
        except _AUTH_ERRORS as e:
            kind = "IMAP hatası" if isinstance(e, imaplib.IMAP4.error) else "Kimlik doğrulama hatası"
            _mark_failed(session, m, f"{kind}: {e}", report)
            report.skipped += len(todo) - i - 1
            report.add_reason(f"{kind.lower()}; kalan mailler gönderilmedi (MAIL_USER / uygulama şifresini kontrol edin)")
            log.error("%s, gönderim durduruldu: %s", kind, e)
            break
        except _CONNECT_ERRORS as e:
            _mark_failed(session, m, f"Bağlantı hatası: {type(e).__name__}: {e}", report)
            report.skipped += len(todo) - i - 1
            report.add_reason("sunucuya bağlanılamadı; kalan mailler gönderilmedi (SMTP_HOST / IMAP_HOST ve ağ bağlantısını kontrol edin)")
            log.error("Bağlantı hatası, gönderim durduruldu: %s", e)
            break
        except Exception as e:  # noqa: BLE001 - tek mail düşerse diğerleri devam eder
            _mark_failed(session, m, f"{type(e).__name__}: {e}", report)
            log.warning("Gönderim hatası [%s]: %s", c.id, e)
        if i < len(todo) - 1:
            delay = random.uniform(settings.send_delay_min_sec, settings.send_delay_max_sec)
            log.info("Sonraki mail için %.0f sn bekleniyor", delay)
            try:
                sleeper(delay)
            except KeyboardInterrupt:
                report.skipped += len(todo) - i - 1
                report.add_reason("kullanıcı durdurdu (Ctrl+C); kalan mailler onaylı olarak bekliyor")
                log.warning("Bekleme sırasında durduruldu; kalan %d mail onaylı kaldı", len(todo) - i - 1)
                break
    if simulate and report.sent:
        report.add_reason("prova: hiçbir mail gönderilmedi" if dry_run else "MAIL_MODE=dry: yalnızca loglandı")
    log.info(report.summary())
    return report
