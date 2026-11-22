"""Gelen mesajı şirkete eşleştirme ve olumlu/olumsuz tahmini. Bkz. MIMARI.md bölüm 8.

Sıra: (1) In-Reply-To / References -> bizim Message-ID (ya da aynı şirkete bağlı önceki bir mesaj);
(2) konu/gövdedeki ``BV-0042`` takip kodu; (3) gönderen (ya da Reply-To) alan adı -> Company.domain ya da
Contact e-posta alan adı; alt alan adları üst alan adına indirgenir (``mail.sirket.com`` -> ``sirket.com``),
genel sağlayıcılar (gmail, hotmail...) hariç; birebir Contact e-postası genel sağlayıcıda da eşleşir.

Otomatik yanıtlar (ofis dışı vb.) eşleştirilir ama "cevap geldi" sayılmaz ve duygu tahmini yapılmaz.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import services
from ..enums import Actor, AppStatus, Direction, EventType, MailStatus, MatchMethod, Sentiment, ascii_fold
from ..log import get_logger
from ..models import Company, Contact, EmailMessage
from ..utils import GENERIC_EMAIL_DOMAINS, domain_of, now_utc

log = get_logger(__name__)

TRACKING_RE = re.compile(r"\bBV-(\d{4,6})\b", re.I)
_MSGID_RE = re.compile(r"<[^<>\s]+>")

# İkinci seviye genel son ekler: tek başına şirket alan adı olamaz ("com.tr" gibi).
PUBLIC_SUFFIXES: frozenset[str] = frozenset(
    {
        "com.tr", "org.tr", "net.tr", "edu.tr", "gov.tr", "k12.tr", "gen.tr", "bel.tr", "av.tr", "dr.tr", "pol.tr",
        "tsk.tr", "web.tr", "tv.tr", "info.tr", "biz.tr", "name.tr", "co.uk", "org.uk", "ac.uk", "com.au", "co.jp",
        "com.br", "co.in", "com.de", "co.za",
    }
)

# Anahtar kelimeler ASCII'ye indirgenmiş (ascii_fold) ve kelime başında aranır; "kontenjan" -> "kontenjanımız" da sayılır.
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "maalesef", "uzgunuz", "uzgunum", "uygun degil", "uygun gorulme", "uygun bulunma", "uygun olmadigi", "kontenjan",
    "ileriki donem", "ilerleyen donem", "sonraki donem", "gelecek donem", "degerlendirilmemis", "degerlendirilememis",
    "olumsuz", "su an icin", "su anda staj", "staj alimimiz yok", "stajyer alimimiz", "alim yapmiyoruz", "alim yapmamakta",
    "almiyoruz", "almamaktayiz", "almamaktadir", "kapali", "kapatilmis", "unfortunately", "regret", "not able", "no open",
    "no vacanc", "basarilar dileriz", "basarilar diler", "basarilar dilerim", "ne yazik ki", "mumkun degil",
    "mumkun olmamakta", "mumkun olmayacak", "olumlu sonuclanmamis", "kabul edilmemis", "kabul edilmedi", "edilmemis",
    "edilmedi", "edilememis", "bulunmamakta", "bulunmuyor", "mevcut degil", "karsilayamiyoruz", "karsilayamamakta",
    "ihtiyacimiz yok", "ihtiyac bulunma", "yer verememekte", "yer veremiyoruz", "dolmustur", "doldugu",
)
POSITIVE_KEYWORDS: tuple[str, ...] = (
    "gorusme", "gorusmek isteriz", "gorusmek istiyoruz", "mulakat", "musait", "telefon", "davet", "ne zaman", "uygun olur",
    "uygun olursunuz", "uygun oldugunuz", "tanismak", "tanisma", "goruselim", "bekliyoruz", "ofisimize", "ofise",
    "online toplanti", "toplanti", "takvim", "randevu", "arayacagiz", "sizi arayalim", "arayalim", "hangi gun",
    "hangi saat", "saat kacta", "interview", "schedule", "available", "call", "meet", "zoom", "teams", "olumlu",
    "memnuniyetle", "degerlendirmeye aldik", "ilgileniyoruz", "kabul", "ziyaret", "gelebilir misiniz", "gelebilirsiniz",
    "baslayabilir", "ne zaman baslaya",
)
INFO_KEYWORDS: tuple[str, ...] = (
    "belge", "transkript", "protokol", "form", "cv", "ozgecmis", "ekte", "iletebilir", "gonderebilir", "gonderir misiniz",
    "paylasabilir", "doldur", "basvuru formu", "staj defteri", "sigorta", "okuldan", "okulunuzdan", "okulunuz", "kimlik",
    "evrak", "bilgi verebilir", "bilgi verir misiniz", "detay", "hangi bolum", "kacinci sinif", "staj tarihleri",
    "tarihleri", "suresi", "sureniz", "ne kadar sure", "kac gun", "kac hafta", "kac ay", "not ortalama", "gano",
    "hangi alan", "hangi teknoloji", "hangi dil",
)
# Nezaket kalıpları: duygu taşımaz, sayılmadan önce silinir ("görüşmek üzere" olumsuz mailde de geçer).
_NEUTRAL_PHRASES: tuple[str, ...] = ("gorusmek uzere", "iyi gunler", "iyi calismalar", "iyi aksamlar", "tesekkur ederiz", "tesekkurler")


def _keyword_re(keywords: tuple[str, ...]) -> re.Pattern[str]:
    # En uzun kalıp önce: aynı konumda "kontenjanimiz" ile "kontenjan" iki kez sayılmaz (örtüşmesiz tarama).
    alts = "|".join(re.escape(k) for k in sorted(set(keywords), key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:" + alts + ")")


_NEG_RE = _keyword_re(NEGATIVE_KEYWORDS)
_POS_RE = _keyword_re(POSITIVE_KEYWORDS)
_INFO_RE = _keyword_re(INFO_KEYWORDS)
_NEUTRAL_RE = _keyword_re(_NEUTRAL_PHRASES)

# Otomatik yanıt (ofis dışı, tatil) tespiti — ascii_fold edilmiş metinde aranır.
_AUTO_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|ynt|yan[ıi]t)\s*:\s*)*(?:otomatik (?:yanit|cevap|mesaj)|automatic reply|auto(?:matic)?[ -]?reply"
    r"|autoreply|auto-?response|out of (?:the )?office|ofis disi|yillik izin|abwesenheit)"
)
_AUTO_BODY_RE = re.compile(
    r"ofis disinda|out of (?:the )?office|otomatik (?:bir )?(?:yanit|cevap|mesaj)|yillik izin|izinde oldugum|izinli oldugum"
    r"|automatic reply|auto-?reply|this is an automated|bu otomatik|otomatik olarak gonderil"
)

_META_ATTR = "_stajbot_inbound_meta"


@dataclass
class InboundMeta:
    """Veritabanında sütunu olmayan ama eşleştirmede işe yarayan başlık bilgileri (ayrıştırma sırasında eklenir)."""

    reply_to: str | None = None
    auto_reply: bool = False


def set_inbound_meta(msg: EmailMessage, meta: InboundMeta) -> None:
    setattr(msg, _META_ATTR, meta)


def inbound_meta(msg: EmailMessage) -> InboundMeta:
    return getattr(msg, _META_ATTR, None) or InboundMeta()


@dataclass
class MatchResult:
    company: Company | None
    method: MatchMethod
    sentiment: Sentiment
    auto_reply: bool = False

    @property
    def matched(self) -> bool:
        return self.company is not None


def extract_tracking_code(text: str | None) -> str | None:
    if not text:
        return None
    m = TRACKING_RE.search(text)
    return f"BV-{m.group(1)}".upper() if m else None


def tracking_code_to_id(code: str | None) -> int | None:
    if not code:
        return None
    m = TRACKING_RE.search(code)
    return int(m.group(1)) if m else None


def _msgids(*headers: str | None) -> list[str]:
    """Başlıklardaki Message-ID'leri ``<...>`` biçiminde döner; köşeli ayraçsız yazılmışsa da yakalar."""
    out: list[str] = []
    for h in headers:
        if not h:
            continue
        found = _MSGID_RE.findall(h)
        if found:
            out.extend(found)
            continue
        for tok in h.split():
            tok = tok.strip().strip(",;")
            if "@" in tok:
                out.append(f"<{tok.strip('<>')}>")
    return list(dict.fromkeys(out))


def _is_quoted_line(line: str) -> bool:
    return line.lstrip().startswith(">")


_QUOTE_HEADER_RE = re.compile(
    r"^(On .+ wrote:|.+ tarihinde .+ yazdı:|.+ tarihinde .+ şunu$|.+ şunu yazdı:|(şunu )?yazdı:$|wrote:$"
    r"|-{3,}\s*(Original Message|Özgün İleti|Orijinal İleti|Forwarded message|İletilen ileti)|From:|Kimden:|Gönderen:|Sent:|Gönderildi:)",
    re.I,
)


def reply_body(text: str | None) -> str:
    """Alıntılanan eski mesajı ve imzayı kabaca atar; yalnızca cevabın kendisi kalır.

    Cevap alıntının altına yazılmışsa (bottom-posting) alıntı satırları atılır, kalan metin döner.
    """
    if not text:
        return ""
    lines: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if _is_quoted_line(ln) or _QUOTE_HEADER_RE.match(s) or s == "--":
            break
        lines.append(ln)
    top = "\n".join(lines).strip()
    if top:
        return top
    rest = [ln for ln in text.splitlines() if not _is_quoted_line(ln) and not _QUOTE_HEADER_RE.match(ln.strip())]
    return "\n".join(rest).strip()


def _folded_reply(text: str | None) -> str:
    return ascii_fold(reply_body(text) or text or "")


def is_auto_reply(subject: str | None, body: str | None = None, *, header_hint: bool = False) -> bool:
    """Ofis dışı / otomatik yanıt mı? Başlık ipucu (Auto-Submitted) kesindir; yoksa konu ve gövde kalıplarına bakar."""
    if header_hint:
        return True
    if _AUTO_SUBJECT_RE.search(ascii_fold(subject or "")):
        return True
    return bool(_AUTO_BODY_RE.search(_folded_reply(body)[:600]))


def guess_sentiment(text: str | None) -> Sentiment:
    """Anahtar kelime sözlüğüyle tahmin; yalnızca ipucu, kullanıcı onaylar. Otomatik yanıt -> unknown."""
    folded = _folded_reply(text)
    if not folded:
        return Sentiment.unknown
    if _AUTO_SUBJECT_RE.search(folded) or _AUTO_BODY_RE.search(folded[:600]):
        return Sentiment.unknown
    folded = _NEUTRAL_RE.sub(" ", folded)
    neg = len(_NEG_RE.findall(folded))
    pos = len(_POS_RE.findall(folded))
    info = len(_INFO_RE.findall(folded))
    if neg and neg >= pos:
        return Sentiment.negative
    if pos and pos >= info:
        return Sentiment.positive
    if info:
        return Sentiment.info
    if pos:
        return Sentiment.positive
    return Sentiment.unknown


# --- teslim edilemedi (bounce) --------------------------------------------------------

_BOUNCE_SENDERS = {"mailer-daemon", "postmaster", "mail-daemon", "maildaemon"}
# ascii_fold edilmiş konu satırında aranır
_BOUNCE_SUBJECT_RE = re.compile(
    r"undeliverable|undelivered|delivery status notification \(failure\)|delivery (?:has )?failed|"
    r"mail delivery (?:failed|failure|subsystem)|returned mail|failure notice|non-?delivery|"
    r"teslim edilemedi|iletilemedi|ulastirilamadi|ileti engellendi"
)
_SMTP_CODE_RE = re.compile(r"\b(?:[45]\.\d{1,3}\.\d{1,3}|5\d\d)\b")
# Satırın başında SMTP kodu: "550 5.4.1 ...", "Error: 554 5.4.14 ...", "Status code 554 ..."
_SMTP_LINE_RE = re.compile(r"^(?:error|status code|remote server returned)?\s*:?\s*'?[45]\d\d\b", re.I)
# ascii_fold edilmiş satırda: insanın okuyacağı açıklama
_BOUNCE_HUMAN_RE = re.compile(r"couldn't be delivered|could not be delivered|wasn't found|not found|does not exist|user unknown|"
                              r"mailbox (?:unavailable|full)|engellendi|teslim edilemedi|bulunamadi|iletilemedi")


def is_bounce(msg: EmailMessage) -> bool:
    """Sunucunun 'mail teslim edilemedi' bildirimi mi? (Gönderen MAILER-DAEMON/postmaster ya da konu kalıbı.)"""
    local = (msg.from_addr or "").split("@", 1)[0].strip().lower()
    if local in _BOUNCE_SENDERS:
        return True
    return bool(_BOUNCE_SUBJECT_RE.search(ascii_fold(msg.subject or "")))


def bounce_reason(text: str | None) -> str:
    """Bildirimden kısa bir sebep çıkarır: okunur açıklama satırı + SMTP hata satırı (hangisi varsa)."""
    lines = [ln.strip(" >*\t") for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if len(ln) > 3 and not ln.startswith("[http")]
    human = next((ln for ln in lines if _BOUNCE_HUMAN_RE.search(ascii_fold(ln))), None)
    code = next((ln for ln in lines if _SMTP_LINE_RE.match(ln)), None) or next((ln for ln in lines if _SMTP_CODE_RE.search(ln)), None)
    parts = [p for p in dict.fromkeys((human, code)) if p]
    return (" — ".join(parts) if parts else (lines[0] if lines else "sebep belirtilmemiş"))[:240]


def _bounced_outbound(msg: EmailMessage, company: Company, session: Session) -> EmailMessage | None:
    """Bildirimin ait olduğu giden mail: başlıklarda adı geçen Message-ID, yoksa şirkete son gönderilen mail."""
    ids = _msgids(msg.in_reply_to, msg.references_header)
    base = select(EmailMessage).where(
        EmailMessage.company_id == company.id, EmailMessage.direction == Direction.outbound, EmailMessage.status == MailStatus.sent
    )
    if ids:
        variants = list(dict.fromkeys(ids + [i.strip("<>") for i in ids]))
        hit = session.scalar(base.where(EmailMessage.message_id.in_(variants)))
        if hit is not None:
            return hit
    return session.scalar(base.order_by(EmailMessage.sent_at.desc(), EmailMessage.id.desc()))


def _handle_bounce(msg: EmailMessage, company: Company, session: Session, when) -> None:
    reason = bounce_reason(msg.body_text)
    out = _bounced_outbound(msg, company, session)
    target = out.to_addr if out is not None else None
    if out is not None:
        out.status = MailStatus.failed
        out.error = f"Teslim edilemedi: {reason}"[:1000]
    services.add_event(
        session, company, EventType.email_received,
        f"Mail ulaşmadı{f' ({target})' if target else ''}"[:255],
        body=reason, actor=Actor.system, email_message=msg, occurred_at=when,
    )
    if company.status in (AppStatus.sent, AppStatus.no_reply, AppStatus.followup_sent):
        services.change_status(
            session, company, AppStatus.bounced, actor=Actor.system,
            title=f"Mail ulaşmadı{f': {target}' if target else ''}"[:255], email_message=msg, occurred_at=when,
        )


# --- eşleştirme adımları -----------------------------------------------------------

def _by_message_id(msg: EmailMessage, session: Session) -> Company | None:
    ids = _msgids(msg.in_reply_to, msg.references_header)
    if not ids:
        return None
    variants = list(dict.fromkeys(ids + [i.strip("<>") for i in ids]))
    stmt = select(EmailMessage).where(EmailMessage.message_id.in_(variants), EmailMessage.company_id.is_not(None))
    if msg.id is not None:
        stmt = stmt.where(EmailMessage.id != msg.id)
    rows = session.scalars(stmt).all()
    if not rows:
        return None
    # bizim gönderdiğimiz mesaj öncelikli; yoksa aynı şirkete bağlanmış önceki gelen mesaj (konu zinciri)
    rows.sort(key=lambda r: (0 if r.direction == Direction.outbound else 1, r.id or 0))
    return rows[0].company


def _by_tracking_code(msg: EmailMessage, session: Session) -> Company | None:
    code = extract_tracking_code(msg.subject) or extract_tracking_code(msg.body_text)
    cid = tracking_code_to_id(code)
    if cid is None:
        return None
    c = session.get(Company, cid)
    if c is not None:
        msg.tracking_code = code
    return c


def _candidate_addrs(msg: EmailMessage) -> list[str]:
    out: list[str] = []
    for a in (msg.from_addr, inbound_meta(msg).reply_to):
        a = (a or "").strip().lower()
        if "@" in a and a not in out:
            out.append(a)
    return out


def parent_domains(dom: str | None) -> list[str]:
    """``ik.beta.com.tr`` -> [``ik.beta.com.tr``, ``beta.com.tr``]; genel son ekler (``com.tr``, ``com``) listeye girmez."""
    if not dom:
        return []
    labels = [p for p in dom.lower().split(".") if p]
    out: list[str] = []
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        if "." not in cand or cand in PUBLIC_SUFFIXES:
            break
        out.append(cand)
    return out


def _by_domain(msg: EmailMessage, session: Session) -> Company | None:
    addrs = _candidate_addrs(msg)
    # birebir kişi e-postası (gmail gibi genel sağlayıcıda olsa bile güçlü kanıt)
    for addr in addrs:
        contact = session.scalar(select(Contact).where(Contact.email == addr).order_by(Contact.id.asc()))
        if contact is not None:
            return contact.company
    for addr in addrs:
        cands = parent_domains(domain_of(addr))
        if not cands or any(c in GENERIC_EMAIL_DOMAINS for c in cands):
            continue
        for cand in cands:
            c = session.scalar(select(Company).where(Company.domain == cand))
            if c is not None:
                return c
        for cand in cands:
            contact = session.scalar(select(Contact).where(Contact.email.like(f"%@{cand}")).order_by(Contact.id.asc()))
            if contact is not None:
                return contact.company
    return None


def match(msg: EmailMessage, session: Session) -> MatchResult:
    """Gelen mesajı şirkete bağlar, duygu tahmini yazar, gerekiyorsa durumu 'replied' yapar."""
    company: Company | None = None
    method = MatchMethod.none
    for fn, m in ((_by_message_id, MatchMethod.message_id), (_by_tracking_code, MatchMethod.tracking_code), (_by_domain, MatchMethod.domain)):
        try:
            company = fn(msg, session)
        except Exception as e:  # noqa: BLE001
            log.warning("Eşleştirme adımı hatası (%s): %s", m.value, e)
            company = None
        if company is not None:
            method = m
            break
    bounce = is_bounce(msg)
    auto = bounce or is_auto_reply(msg.subject, msg.body_text, header_hint=inbound_meta(msg).auto_reply)
    sentiment = Sentiment.unknown if auto else guess_sentiment((msg.subject or "") + "\n" + (msg.body_text or ""))
    msg.sentiment = sentiment
    msg.matched_by = method
    if company is None:
        log.info("Sahipsiz gelen mail: %s | %s", msg.from_addr, msg.subject)
        return MatchResult(None, method, sentiment, auto)
    msg.company = company
    if method != MatchMethod.tracking_code or not msg.tracking_code:
        msg.tracking_code = company.tracking_code
    when = msg.received_at or now_utc()
    if company.last_contact_at is None or when > company.last_contact_at:
        company.last_contact_at = when
    if bounce:
        _handle_bounce(msg, company, session, when)
        session.flush()
        log.warning("Mail ulaşmadı [%s] %s (%s)", company.id, company.name, method.value)
        return MatchResult(company, method, sentiment, auto)
    prefix = "Otomatik yanıt: " if auto else "Gelen mail: "
    services.add_event(
        session, company, EventType.email_received, (prefix + (msg.subject or "(konu yok)"))[:255],
        body=(reply_body(msg.body_text) or msg.body_text or "")[:2000], actor=Actor.system, email_message=msg, occurred_at=when,
    )
    # bounced da dahil: bir adres ulaşmasa bile (ör. info@ iç adrese yönlendirip geri döner) şirket başka adresten cevap verebilir.
    if not auto and company.status in (AppStatus.sent, AppStatus.no_reply, AppStatus.followup_sent, AppStatus.bounced):
        services.change_status(
            session, company, AppStatus.replied, actor=Actor.system,
            title=f"Cevap geldi (tahmin: {sentiment.value})", email_message=msg, occurred_at=when,
        )
    session.flush()
    log.info("Gelen mail eşleşti [%s] %s (%s, tahmin: %s%s)", company.id, company.name, method.value, sentiment.value, ", otomatik yanıt" if auto else "")
    return MatchResult(company, method, sentiment, auto)
