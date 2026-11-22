"""IMAP okuma ve gelen mesajı kaydetme. Bkz. MIMARI.md bölüm 8.

``fetch_new(session)``: INBOX'ta ``AppSetting['imap_last_uid']`` sonrasındaki UID'leri çeker, her birini
``EmailMessage(direction=inbound, status=received)`` olarak kaydeder (Message-ID ile tekil) ve
``matcher.match`` ile şirkete bağlar. Hiç exception fırlatmaz; bağlantı hatasında boş liste döner.

Dayanıklılık kuralları:
- Her mesaj kendi SAVEPOINT'i içinde işlenir; biri patlarsa yarım kayıt kalmaz.
- ``imap_last_uid`` yalnızca başarıyla işlenen (ya da bilinçli atlanan) UID'ye kadar ilerler. İşlenemeyen mesaj
  bir sonraki çalıştırmada bir kez daha denenir; yine patlarsa loglanır ve atlanır (``imap_failed_uid``).
- Kendi adresimizden gelenler atlanır; Message-ID (ya da UID) daha önce kaydedilmişse yeniden yazılmaz.
"""
from __future__ import annotations

import email
import html as _html
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_setting, set_setting
from ..enums import Direction, MailStatus
from ..log import get_logger
from ..models import EmailMessage
from ..utils import clean_text, now_utc
from . import matcher
from .matcher import InboundMeta, inbound_meta  # noqa: F401 - dışa açık yardımcılar

log = get_logger(__name__)

LAST_UID_KEY = "imap_last_uid"
FAILED_UID_KEY = "imap_failed_uid"
MAX_PER_RUN = 200
# İlk başvuru mailinden bu kadar gün öncesine kadar bakılır; hiç mail gönderilmemişse bugünden geriye.
SINCE_MARGIN_DAYS = 1
NO_SENT_LOOKBACK_DAYS = 7
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_TAG_RE = re.compile(r"<[^>]+>")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Alıntı başlangıcı: Gmail/Outlook/Yahoo/Thunderbird cevap blokları. Bu noktadan sonrası "> " ile işaretlenir.
_QUOTE_START_RE = re.compile(
    r"<blockquote\b|<div\b[^>]*\b(?:class|id)\s*=\s*[\"']?[^\"'>]*(?:gmail_quote|appendonsend|divRplyFwdMsg|yahoo_quoted|moz-cite-prefix|OutlookMessageHeader)",
    re.I,
)
# Türkçe postalarda sık görülen yanlış/boş charset bildirimleri: önce UTF-8, sonra cp1254 denenir.
_WESTERN_CHARSETS = {"iso-8859-1", "iso8859-1", "latin-1", "latin1", "windows-1252", "cp1252", "us-ascii", "ascii", "iso-8859-15"}
_FALLBACK_CHARSETS = ("utf-8", "cp1254", "latin-1")


class _FetchError(Exception):
    """IMAP FETCH başarısız (sunucu NO/BAD döndü ya da gövde yok)."""


# --- başlıklar ---------------------------------------------------------------------

def _decode_header(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return clean_text(str(value), 512)
    except Exception:  # noqa: BLE001
        return None


def _addresses(msg: Message, name: str) -> list[str]:
    """Başlıktaki e-posta adreslerini küçük harfle döner. Bozuk başlıkta (tırnaksız virgüllü ad vb.) regex ile kurtarır."""
    out: list[str] = []
    try:
        values = msg.get_all(name) or []
    except Exception:  # noqa: BLE001
        values = []
    for v in values:
        found: list[str] = []
        for a in getattr(v, "addresses", ()) or ():
            spec = (getattr(a, "addr_spec", "") or "").strip().lower()
            if "@" in spec and _EMAIL_RE.fullmatch(spec):
                found.append(spec)
        if not found:
            try:
                found = [m.lower() for m in _EMAIL_RE.findall(str(v))]
            except Exception:  # noqa: BLE001
                found = []
        for f in found:
            if f not in out:
                out.append(f)
    return out


def _normalize_msgid(value: Any) -> str | None:
    s = _decode_header(value)
    if not s:
        return None
    s = s.strip()
    if "<" not in s and "@" in s:
        s = f"<{s.strip('<>')}>"
    return s


def _is_auto_submitted(msg: Message) -> bool:
    try:
        auto = str(msg.get("Auto-Submitted") or "").strip().lower()
        if auto and not auto.startswith("no"):
            return True
        if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
            return True
        return str(msg.get("Precedence") or "").strip().lower() in {"auto_reply", "auto-reply"}
    except Exception:  # noqa: BLE001
        return False


# --- gövde --------------------------------------------------------------------------

def _fragment_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|head|title)\b.*?</\1\s*>", " ", html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>|</blockquote>|</table>|</pre>", "\n", text)
    text = _html.unescape(_TAG_RE.sub(" ", text)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """HTML'i düz metne çevirir; alıntılanan eski mesaj (blockquote, gmail_quote...) satırları ``> `` ile işaretlenir."""
    m = _QUOTE_START_RE.search(html)
    head, quoted = (html[: m.start()], html[m.start():]) if m else (html, "")
    text = _fragment_to_text(head)
    if quoted:
        q = _fragment_to_text(quoted)
        if q:
            marked = "\n".join(("> " + ln) if ln else ">" for ln in q.splitlines())
            text = f"{text}\n\n{marked}" if text else marked
    return text.strip()


def _decode_bytes(payload: bytes, charset: str | None) -> str:
    declared = (charset or "").strip().lower()
    order: list[str] = []
    if declared and declared not in _WESTERN_CHARSETS:
        order.append(declared)
    order.extend(_FALLBACK_CHARSETS)
    for cs in dict.fromkeys(order):
        try:
            return payload.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _part_text(part: Message) -> str | None:
    payload = part.get_payload(decode=True)
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    return _decode_bytes(bytes(payload), part.get_content_charset())


def _find_body(msg: Message, kind: str) -> Message | None:
    try:
        return msg.get_body(preferencelist=(kind,))
    except Exception as e:  # noqa: BLE001
        log.debug("Gövde (%s) seçilemedi: %s", kind, e)
        return None


def _bodies(msg: Message) -> tuple[str | None, str | None]:
    text, html = None, None
    plain = _find_body(msg, "plain")
    if plain is not None:
        try:
            text = _part_text(plain)
        except Exception as e:  # noqa: BLE001
            log.debug("Düz metin gövdesi ayrıştırılamadı: %s", e)
    hpart = _find_body(msg, "html")
    if hpart is not None:
        try:
            html = _part_text(hpart)
        except Exception as e:  # noqa: BLE001
            log.debug("HTML gövdesi ayrıştırılamadı: %s", e)
    if text is None and html:
        text = html_to_text(html)
    if text is None and html is None and not msg.is_multipart():
        try:
            text = _part_text(msg)
        except Exception:  # noqa: BLE001
            text = None
    text = text.strip() if isinstance(text, str) else None
    return (text or None), (html if isinstance(html, str) and html.strip() else None)


def _received_at(msg: Message) -> datetime:
    try:
        dt = parsedate_to_datetime(str(msg.get("Date")))
        if dt is not None:
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    return now_utc()


def parse_message(raw: bytes, uid: int | None = None) -> EmailMessage:
    """Ham RFC822 baytlarını EmailMessage (inbound, received) nesnesine çevirir (veritabanına yazmaz).

    Reply-To ve otomatik yanıt ipucu sütun olmadığı için ``matcher.inbound_meta(msg)`` ile taşınır.
    """
    m = email.message_from_bytes(raw, policy=policy.default)
    text, html = _bodies(m)
    froms = _addresses(m, "From")
    tos = _addresses(m, "To")
    ccs = _addresses(m, "Cc")
    reply_tos = _addresses(m, "Reply-To")
    subject = _decode_header(m.get("Subject"))
    msg = EmailMessage(
        direction=Direction.inbound,
        status=MailStatus.received,
        message_id=_normalize_msgid(m.get("Message-ID")),
        in_reply_to=_decode_header(m.get("In-Reply-To")),
        references_header=(str(m.get("References")) if m.get("References") else None),
        from_addr=(froms[0][:255] if froms else None),
        to_addr=(tos[0][:255] if tos else None),
        cc=(", ".join(ccs)[:512] if ccs else None),
        subject=subject,
        body_text=text,
        body_html=html,
        imap_uid=uid,
        received_at=_received_at(m),
        tracking_code=matcher.extract_tracking_code(subject) or matcher.extract_tracking_code(text),
    )
    matcher.set_inbound_meta(msg, InboundMeta(reply_to=(reply_tos[0] if reply_tos else None), auto_reply=_is_auto_submitted(m)))
    return msg


# --- kayıt --------------------------------------------------------------------------

def _exists(session: Session, msg: EmailMessage) -> bool:
    if msg.message_id:
        if session.scalar(select(EmailMessage.id).where(EmailMessage.message_id == msg.message_id, EmailMessage.direction == Direction.inbound)) is not None:
            return True
    if msg.imap_uid is not None:
        return session.scalar(select(EmailMessage.id).where(EmailMessage.imap_uid == msg.imap_uid, EmailMessage.direction == Direction.inbound)) is not None
    return False


def store_message(msg: EmailMessage, session: Session) -> EmailMessage | None:
    """Mesajı kaydeder ve eşleştirir; zaten varsa None döner."""
    if _exists(session, msg):
        return None
    session.add(msg)
    session.flush()
    matcher.match(msg, session)
    return msg


def _imap_connect(settings: Settings, factory: Callable[..., Any] | None):
    factory = factory or imaplib.IMAP4_SSL
    imap = factory(settings.imap_host, settings.imap_port)
    imap.login(settings.mail_user, settings.mail_password)
    return imap


def _int_setting(session: Session, key: str) -> int:
    try:
        return int(get_setting(session, key, "0") or 0)
    except (TypeError, ValueError):
        return 0


def _imap_date(dt: datetime) -> str:
    """IMAP SEARCH tarih biçimi (dil ayarından bağımsız): 19-Nov-2026."""
    return f"{dt.day}-{_IMAP_MONTHS[dt.month - 1]}-{dt.year}"


def since_date(session: Session) -> datetime:
    """Gelen kutusunda bakılacak en eski tarih.

    Başvurulara cevap ancak ilk başvuru gönderildikten sonra gelebilir; bu yüzden kişisel gelen kutusunun
    eski geçmişi (yıllarca mail) hiç okunmaz. Hiç gönderilmiş mail yoksa son birkaç gün.
    """
    first_sent = session.scalar(
        select(func.min(EmailMessage.sent_at)).where(EmailMessage.direction == Direction.outbound, EmailMessage.sent_at.is_not(None))
    )
    if first_sent is not None:
        return first_sent - timedelta(days=SINCE_MARGIN_DAYS)
    return now_utc() - timedelta(days=NO_SENT_LOOKBACK_DAYS)


def _raw_from_fetch(parts: Any) -> bytes | None:
    for part in parts or []:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])
    return None


def fetch_new(
    session: Session,
    *,
    imap_factory: Callable[..., Any] | None = None,
    folder: str = "INBOX",
    max_messages: int | None = None,
) -> list[EmailMessage]:
    """IMAP'tan yeni mesajları alır, kaydeder, eşleştirir. Hata -> log + boş liste.

    Yalnızca ``since_date`` (ilk başvuru mailinden bir gün önce) ve sonrasındaki mesajlar okunur.
    ``max_messages`` (varsayılan ``MAX_PER_RUN``) bir çalıştırmada işlenecek en çok mesaj sayısıdır; kalanlar
    sonraki çalıştırmada devam eder (en eski UID önce).
    """
    settings = get_settings()
    if not settings.mail_configured:
        log.error("MAIL_USER / MAIL_PASSWORD tanımlı değil; gelen kutusu okunamaz.")
        return []
    limit = MAX_PER_RUN if max_messages is None else max(1, int(max_messages))
    last_uid = _int_setting(session, LAST_UID_KEY)
    prev_failed = _int_setting(session, FAILED_UID_KEY)
    own_addr = (settings.mail_user or "").strip().lower()
    new: list[EmailMessage] = []
    try:
        imap = _imap_connect(settings, imap_factory)
    except Exception as e:  # noqa: BLE001
        log.error("IMAP bağlantısı kurulamadı (%s): %s", settings.imap_host, e)
        return []
    try:
        typ, _ = imap.select(folder, readonly=True)
        if typ != "OK":
            log.error("IMAP klasörü açılamadı: %s", folder)
            return []
        since = _imap_date(since_date(session))
        typ, data = imap.uid("search", None, f"UID {last_uid + 1}:* SINCE {since}")
        if typ != "OK" or not data or not data[0]:
            if typ != "OK":
                log.error("IMAP UID araması başarısız: %s", typ)
            return []
        # 'UID n:*' n en büyük UID'den büyükse sunucu en son mesajı yine döner -> last_uid'den küçük/eşitler atılır
        uids = sorted({int(u) for u in data[0].split() if u.isdigit() and int(u) > last_uid})
        if len(uids) > limit:
            log.warning("%d yeni mesaj var; bu çalıştırmada ilk %d işleniyor", len(uids), limit)
            uids = uids[:limit]
        max_seen = last_uid
        failed_uid = 0
        for uid in uids:
            try:
                typ, parts = imap.uid("fetch", str(uid), "(RFC822)")
                raw = _raw_from_fetch(parts) if typ == "OK" else None
                if raw is None:
                    raise _FetchError(f"sunucu mesaj gövdesini vermedi ({typ})")
                msg = parse_message(raw, uid)
                if own_addr and msg.from_addr == own_addr:
                    log.debug("Kendi gönderdiğimiz mesaj atlandı (UID %s)", uid)
                else:
                    with session.begin_nested():
                        stored = store_message(msg, session)
                    if stored is not None:
                        new.append(stored)
            except Exception as e:  # noqa: BLE001 - tek mesaj bozuksa akış durmaz
                if uid == prev_failed:
                    log.error("UID %s ikinci denemede de işlenemedi, atlanıyor: %s", uid, e)
                else:
                    log.warning("UID %s işlenemedi (%s); sonraki çalıştırmada yeniden denenecek", uid, e)
                    failed_uid = uid
                    break
            max_seen = uid
        if max_seen != last_uid:
            set_setting(session, LAST_UID_KEY, str(max_seen))
        if failed_uid != prev_failed:
            set_setting(session, FAILED_UID_KEY, str(failed_uid))
        session.flush()
    except Exception as e:  # noqa: BLE001
        log.error("Gelen kutusu okunurken hata: %s", e)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    log.info("Gelen kutusu: %d yeni mesaj", len(new))
    return new
