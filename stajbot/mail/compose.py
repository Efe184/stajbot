"""Şablon + LLM paragrafı -> EmailMessage (draft). Bkz. MIMARI.md bölüm 8.

- ``pick_template(company)`` sektör / etiket / büyüklüğe göre şablon seçer.
- ``compose_for_company`` birincil kişiye başvuru taslağı üretir; eski taslak varsa üzerine yazar;
  company ``ready`` ise ``drafted`` olur.
- ``compose_followup`` hatırlatma taslağı üretir (``followup.txt.j2``), önceki gönderime yanıt zinciri kurar.
- ``regenerate_paragraph`` yalnızca "neden bu şirket" paragrafını yeniler; kullanıcının gövdede yaptığı
  diğer düzenlemeleri korur.

Şablonlara verilen yardımcı filtreler (Türkçe ses uyumu): ``gen`` (-in: "Aselsan'ın"), ``loc`` (-de: "İzmir'de"),
``dat`` (-e: "Trendyol'a"); ``short_name`` hukuki ekleri atar ("Örnek Yazılım A.Ş." -> "Örnek Yazılım");
``trdate`` ISO tarihi gün.ay.yıl yapar; ``listele`` "a, b ve c" biçiminde birleştirir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import services
from ..config import get_settings
from ..enums import NO_MAIL_STATUSES, Actor, AppStatus, Direction, EventType, MailStatus, ReviewStatus, SizeBucket, ascii_fold
from ..enrich import keywords
from ..log import get_logger
from ..models import Company, EmailMessage
from . import llm

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAMES = ("default", "startup", "corporate", "defense", "game", "followup")

# Hiç mail gönderilmemiş durumlar: hatırlatma üretilmez.
_NOT_YET_SENT_STATUSES = {AppStatus.found, AppStatus.ready, AppStatus.drafted, AppStatus.queued}

# "Ad Soyad" .env'de doldurulmamış varsayılan değerdir.
_PLACEHOLDER_NAME = "ad soyad"


# --- Türkçe ek yardımcıları ------------------------------------------------------------

_VOWELS = "aeıioöuü"
_VOICELESS = "fstkçşhp"
_GENITIVE = {"a": "ın", "ı": "ın", "e": "in", "i": "in", "o": "un", "u": "un", "ö": "ün", "ü": "ün"}
_DATIVE = {"a": "a", "ı": "a", "e": "e", "i": "e", "o": "a", "u": "a", "ö": "e", "ü": "e"}
# son rakam -> (okunuşunun son ünlüsü, ünlüyle bitiyor mu)
_DIGIT_SOUND = {"0": ("ı", False), "1": ("i", False), "2": ("i", True), "3": ("ü", False), "4": ("ö", False),
                "5": ("e", False), "6": ("ı", True), "7": ("i", True), "8": ("i", False), "9": ("u", False)}

_LEGAL_TAIL = frozenset(
    {"a.s", "as", "anonim", "sirketi", "sti", "ltd", "ltd.sti", "limited", "san", "tic", "ve", "sanayi", "ticaret",
     "inc", "llc", "gmbh", "co", "corp", "s.a", "ag", "plc"}
)
# Soldan sağa ilk görüldüğü yerde adı kesen hukuki belirteçler ("... A.Ş. Teknopark Şubesi" -> "...")
_LEGAL_CUT = frozenset({"anonim", "sirketi", "a.s", "as", "ltd", "ltd.sti", "sti", "limited", "san", "tic", "sanayi",
                        "ticaret", "t.a.o", "tao", "inc", "llc", "gmbh", "corp", "plc", "subesi"})
# Sondan atılan bağlaç/ek sözcükler
_TAIL_FILLER = frozenset({"ve", "ic", "dis", "ith", "ihr", "ithalat", "ihracat", "pazarlama", "ve."})


def _tr_lower(text: str) -> str:
    return text.replace("I", "ı").replace("İ", "i").lower()


def _stem(name: str) -> str:
    return (name or "").strip().rstrip(" .,;:)\"'»”’")


def _last_sound(stem: str) -> tuple[str, bool]:
    """Ekin uyacağı son ünlü ve sözcüğün ünlüyle bitip bitmediği.

    Harfleriyle okunan kısaltmalar (STM, TCDD) 'e' ile biter sayılır; rakamlar okunuşuna göre ele alınır.
    """
    if not stem:
        return "e", True
    token = _tr_lower(stem.split()[-1])
    last = token[-1]
    if last.isdigit():
        return _DIGIT_SOUND[last]
    vowels = [ch for ch in token if ch in _VOWELS]
    if not vowels:
        return "e", True  # harf harf okunan kısaltma: STM'nin, STM'de
    return vowels[-1], last in _VOWELS


def tr_genitive(name: str) -> str:
    """"Aselsan" -> "Aselsan'ın", "Getir" -> "Getir'in", "Konya" -> "Konya'nın", "STM" -> "STM'nin"."""
    stem = _stem(name)
    if not stem:
        return ""
    vowel, ends_vowel = _last_sound(stem)
    suffix = _GENITIVE[vowel]
    return f"{stem}'{'n' + suffix if ends_vowel else suffix}"


def tr_locative(name: str) -> str:
    """"İzmir" -> "İzmir'de", "Konya" -> "Konya'da", "TÜBİTAK" -> "TÜBİTAK'ta", "Getir" -> "Getir'de"."""
    stem = _stem(name)
    if not stem:
        return ""
    vowel, _ = _last_sound(stem)
    last = _tr_lower(stem)[-1]
    consonant = "t" if last in _VOICELESS else "d"
    return f"{stem}'{consonant}{'e' if vowel in 'eiöü' else 'a'}"


def tr_dative(name: str) -> str:
    """"Trendyol" -> "Trendyol'a", "Getir" -> "Getir'e", "Konya" -> "Konya'ya"."""
    stem = _stem(name)
    if not stem:
        return ""
    vowel, ends_vowel = _last_sound(stem)
    suffix = _DATIVE[vowel]
    return f"{stem}'{'y' + suffix if ends_vowel else suffix}"


def _fold_parts(token: str) -> list[str]:
    return [p for p in re.split(r"[.,]", ascii_fold(token).strip(".,;:-()")) if p]


def _is_legal_token(token: str) -> bool:
    """"A.Ş.", "Ltd.", bitişik "SAN.TİC.LTD.ŞTİ" ya da "T.A.O" gibi hukuki belirteç mi?"""
    folded = ascii_fold(token).strip(".,;:-")
    if folded in _LEGAL_CUT or folded in _LEGAL_TAIL:
        return folded not in {"ve", "co", "ag", "s.a"} or folded == "s.a"
    parts = _fold_parts(token)
    if len(parts) > 1 and all(p in _LEGAL_CUT or p in _LEGAL_TAIL or len(p) == 1 for p in parts):
        return True
    return False


def _is_tail_junk(token: str) -> bool:
    """Sonda kalan bağlaç, kısaltma ("Müh.", "Dan.") ya da hukuki ek."""
    folded = ascii_fold(token).strip(",;:-")
    return (
        _is_legal_token(token)
        or folded.strip(".") in _TAIL_FILLER
        or folded.strip(".") in _LEGAL_TAIL
        or (folded.endswith(".") and len(folded) <= 7)
    )


_FRONT_VOWELS = set("eiöüEÖÜ")
_ALL_VOWELS = set("aeıioöuüAEIİOÖUÜ")


def _tr_title_word(word: str) -> str:
    core = word.strip(".,()-&/")
    if not core:
        return word
    if ascii_fold(core) == "ve":
        return "ve"
    has_vowel = any(ch in _ALL_VOWELS for ch in core)
    if any(ch.isdigit() for ch in core) or not has_vowel or len(core) <= 2 or "." in core:
        return word  # STM, NSS, ON2, AI gibi kısaltmalar olduğu gibi
    # Büyük "I": Türkçe sözcükte "ı", yabancı sözcükte (SIEMENS, SOLUTION) "i"
    letters = []
    for i, ch in enumerate(word):
        if ch == "I":
            nb = word[i - 1:i] + word[i + 1:i + 2]
            foreign = any(c in _FRONT_VOWELS or c in "Iİ" for c in core.replace("I", "")) or any(c in _ALL_VOWELS for c in nb)
            letters.append("i" if foreign else "ı")
        elif ch == "İ":
            letters.append("i")
        else:
            letters.append(ch.lower())
    low = "".join(letters)
    for i, ch in enumerate(low):
        if ch.isalpha():
            up = "İ" if ch == "i" else ("I" if ch == "ı" else ch.upper())
            return low[:i] + up + low[i + 1:]
    return low


def _fix_case(text: str) -> str:
    """Tamamı büyük harfli adı Türkçe kurallarla başlık biçimine çevirir; karışık yazılanlara dokunmaz."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters or sum(ch.isupper() for ch in letters) / len(letters) < 0.8:
        return text
    return " ".join(_tr_title_word(w) for w in text.split())


def short_name(name: str) -> str:
    """Hukuki ekleri ve şube bilgisini atar, BÜYÜK HARF adı düzeltir.

    "ASELSAN ELEKTRONİK SAN. VE TİC. A.Ş." -> "Aselsan Elektronik";
    "NETAŞ TELEKOMÜNİKASYON ANONİM ŞİRKETİ ANKARA ŞUBESİ" -> "Netaş Telekomünikasyon".
    """
    original = (name or "").strip()
    tokens = re.sub(r"\bA\.\s+[ŞS]\.", "A.Ş.", original).split()
    for i in range(1, len(tokens)):
        if _is_legal_token(tokens[i]):
            tokens = tokens[:i]
            break
    while len(tokens) > 1 and _is_tail_junk(tokens[-1]):
        tokens.pop()
    joined = " ".join(tokens)
    out = (joined.rstrip(" ,-.") if joined != original else joined) or original
    return _fix_case(out)


def tr_date(value: str | None) -> str:
    """'2027-02-15' -> '15.02.2027'; tanınmayan biçim olduğu gibi döner."""
    s = (value or "").strip()
    try:
        return date.fromisoformat(s).strftime("%d.%m.%Y")
    except ValueError:
        return s


def join_tr(items) -> str:
    """['a', 'b', 'c'] -> 'a, b ve c'."""
    items = [str(x) for x in (items or []) if str(x).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " ve " + items[-1]


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(default=False, default_for_string=False),
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=False,
)
_env.filters.update(gen=tr_genitive, loc=tr_locative, dat=tr_dative, short_name=short_name, trdate=tr_date, listele=join_tr)


@dataclass
class ComposeResult:
    ok: bool
    message: EmailMessage | None = None
    template_name: str | None = None
    error: str | None = None
    used_llm: bool = False
    note: str | None = None


def available_templates() -> list[str]:
    return [p.name[: -len(".txt.j2")] for p in sorted(TEMPLATES_DIR.glob("*.txt.j2"))]


_DEFENSE_RE = re.compile(r"\b(savunma|defen[cs]e|havacilik|aerospace|askeri|radar|military)\b")
_GAME_RE = re.compile(r"\b(oyun|game|games|gaming)\b")
_STARTUP_RE = re.compile(r"\b(startup|start-up|girisim)\b")
_CORPORATE_RE = re.compile(r"\b(kurumsal|holding)\b")


def pick_template(company: Company) -> str:
    """Sektör / etiketler / büyüklüğe göre şablon adı."""
    blob = ascii_fold(" ".join(filter(None, [company.sector or "", " ".join(company.tags or []), " ".join(company.tech_stack or []), (company.description or "")[:300]])))
    if _DEFENSE_RE.search(blob):
        return "defense"
    if _GAME_RE.search(blob):
        return "game"
    if _STARTUP_RE.search(blob) or company.size_bucket in (SizeBucket.s1_10, SizeBucket.s11_50):
        return "startup"
    if company.size_bucket in (SizeBucket.s201_1000, SizeBucket.s1000_plus) or company.is_arge_center or _CORPORATE_RE.search(blob):
        return "corporate"
    return "default"


def build_subject(company: Company) -> str:
    return f"Uzun Dönem Staj Başvurusu - {get_settings().user_full_name} [{company.tracking_code}]"


def render(template_name: str, **ctx) -> str:
    try:
        tmpl = _env.get_template(f"{template_name}.txt.j2")
    except TemplateNotFound:
        log.warning("Şablon bulunamadı: %s; default kullanılıyor", template_name)
        tmpl = _env.get_template("default.txt.j2")
    text = tmpl.render(**ctx)
    # fazla boş satırları ve çift boşlukları sadeleştir
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.strip().splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln == "" and out and out[-1] == "":
            continue
        out.append(ln)
    return "\n".join(out).strip() + "\n"


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*\S)\s*$")


def profile_projects(profile: str | None, limit: int = 3) -> list[str]:
    """Profildeki 'Projeler' başlığı altındaki madde başlıklarını döner (en çok ``limit``).

    "**Ad**: açıklama" ya da "Ad - açıklama" biçimindeki maddelerden yalnızca ad kısmı alınır.
    """
    if not profile:
        return []
    out: list[str] = []
    in_section = False
    for raw in profile.splitlines():
        m = _HEADING_RE.match(raw)
        if m:
            in_section = "proje" in ascii_fold(m.group(1))
            continue
        if not in_section:
            continue
        b = _BULLET_RE.match(raw)
        if not b:
            continue
        item = b.group(1).replace("**", "").replace("__", "").strip("*_ ")
        item = re.split(r"\s*(?::|\s[-–—]\s|\()\s*", item, maxsplit=1)[0].strip(" .:-")
        if 2 <= len(item) <= 60 and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _context(company: Company, *, why_paragraph: str | None, profile: str, extra: dict | None = None) -> dict:
    settings = get_settings()
    contact = company.primary_contact
    techs = keywords.profile_technologies(profile)[:6] if profile else []
    attachments = settings.abs_attachments
    start, end = tr_date(settings.internship_start), tr_date(settings.internship_end)
    ctx = {
        "company": company,
        "short_name": short_name(company.name),
        "contact_name": (contact.name.strip() if contact and contact.name and contact.name.strip() else None),
        "profile": profile,
        "techs": techs,
        "projects": profile_projects(profile),
        "why_paragraph": why_paragraph or "",
        "settings": settings,
        "tracking_code": company.tracking_code,
        "has_attachment": bool(attachments),
        "attachment_names": [p.name for p in attachments],
        "term_dates": f"{start} – {end}" if start and end else "",
    }
    ctx.update(extra or {})
    return ctx


def _existing_draft(session: Session, company: Company) -> EmailMessage | None:
    return session.scalar(
        select(EmailMessage)
        .where(EmailMessage.company_id == company.id, EmailMessage.direction == Direction.outbound, EmailMessage.status == MailStatus.draft)
        .order_by(EmailMessage.created_at.desc())
    )


def _last_sent(session: Session, company: Company) -> EmailMessage | None:
    return session.scalar(
        select(EmailMessage)
        .where(EmailMessage.company_id == company.id, EmailMessage.direction == Direction.outbound, EmailMessage.status == MailStatus.sent)
        .order_by(EmailMessage.sent_at.desc().nulls_last(), EmailMessage.id.desc())
    )


def _precheck(company: Company) -> str | None:
    if company.blacklisted:
        return "Şirket kara listede; taslak üretilmez."
    if company.status in NO_MAIL_STATUSES:
        return f"Şirket durumu '{company.status.value}'; bu durumda mail üretilmez."
    if company.primary_contact is None:
        return "Birincil e-posta adresi yok; önce bir kişi ekleyin (zenginleştirme ya da elle)."
    return None


def _precheck_apply(company: Company) -> str | None:
    err = _precheck(company)
    if err:
        return err
    if company.review_status != ReviewStatus.approved:
        return "Şirket henüz onaylanmadı (inceleme kuyruğunda); önce /inceleme sayfasından onaylayın."
    return None


def _warn_settings() -> None:
    s = get_settings()
    if _tr_lower(s.user_full_name.strip()) in ("", _PLACEHOLDER_NAME):
        log.warning("USER_FULL_NAME .env içinde doldurulmamış; mail 'Ad Soyad' imzasıyla üretiliyor.")
    if not s.user_school.strip():
        log.warning("USER_SCHOOL .env içinde boş; mailde okul adı geçmeyecek.")


def _why_paragraph(company: Company, profile: str, *, use_llm: bool) -> tuple[str | None, bool]:
    if not use_llm:
        return None, False
    settings = get_settings()
    if not settings.llm_configured:
        log.info("ANTHROPIC_API_KEY yok; şablon paragrafı kullanılıyor [%s]", company.id)
        return None, False
    if not profile.strip():
        log.warning("Profil boş; LLM paragrafı üretilmeden şablon paragrafı kullanılıyor [%s]", company.id)
        return None, False
    try:
        return llm.generate_why_paragraph(company, profile), True
    except llm.LLMError as e:
        log.warning("LLM paragrafı üretilemedi, şablon paragrafı kullanılıyor [%s]: %s", company.id, e)
        return None, False


def compose_for_company(company: Company, session: Session, *, use_llm: bool = True) -> ComposeResult:
    """Başvuru maili taslağı üretir ve kaydeder (draft). Alıcı yoksa ya da şirket onaylı değilse ok=False."""
    err = _precheck_apply(company)
    if err:
        return ComposeResult(ok=False, error=err)
    _warn_settings()
    contact = company.primary_contact
    profile = llm.load_profile(required=False)
    why, used_llm = _why_paragraph(company, profile, use_llm=use_llm)
    template_name = pick_template(company)
    body = render(template_name, **_context(company, why_paragraph=why, profile=profile))
    settings = get_settings()

    msg = _existing_draft(session, company)
    is_new = msg is None
    if msg is None:
        msg = EmailMessage(company=company, direction=Direction.outbound, status=MailStatus.draft)
        session.add(msg)
    msg.to_addr = contact.email
    msg.from_addr = settings.mail_user or None
    msg.subject = build_subject(company)
    msg.body_text = body
    msg.body_html = None
    msg.tracking_code = company.tracking_code
    msg.template_name = template_name
    msg.llm_paragraph = why
    msg.error = None
    msg.in_reply_to = None
    msg.references_header = None
    session.flush()

    if company.status == AppStatus.found:
        services.maybe_mark_ready(session, company)
    if company.status == AppStatus.ready:
        services.change_status(session, company, AppStatus.drafted, actor=Actor.system, title=f"Taslak üretildi ({template_name})", email_message=msg)
    else:
        title = f"Taslak {'üretildi' if is_new else 'yeniden üretildi'} ({template_name})"
        services.add_event(session, company, EventType.manual, title, actor=Actor.system, email_message=msg)
    session.flush()
    log.info("Taslak hazır [%s] %s -> %s (%s, LLM: %s)", company.id, company.name, msg.to_addr, template_name, "evet" if used_llm else "hayır")
    return ComposeResult(ok=True, message=msg, template_name=template_name, used_llm=used_llm)


def compose_followup(company: Company, session: Session) -> ComposeResult:
    """Hatırlatma taslağı (followup.txt.j2). Önceki gönderime In-Reply-To/References ile bağlanır.

    Hiç mail gönderilmemiş (found/ready/drafted/queued) şirkete hatırlatma üretilmez.
    """
    err = _precheck(company)
    if err:
        return ComposeResult(ok=False, error=err)
    if company.status in _NOT_YET_SENT_STATUSES:
        return ComposeResult(ok=False, error=f"Bu şirkete henüz başvuru maili gönderilmedi (durum: {company.status.value}); hatırlatma üretilmez.")
    _warn_settings()
    contact = company.primary_contact
    profile = llm.load_profile(required=False)
    last = _last_sent(session, company)
    first_sent_at = last.sent_at.strftime("%d.%m.%Y") if last and last.sent_at else None
    body = render("followup", **_context(company, why_paragraph=None, profile=profile, extra={"first_sent_at": first_sent_at}))
    settings = get_settings()

    msg = _existing_draft(session, company)
    is_new = msg is None
    if msg is None:
        msg = EmailMessage(company=company, direction=Direction.outbound, status=MailStatus.draft)
        session.add(msg)
    msg.to_addr = contact.email
    msg.from_addr = settings.mail_user or None
    base_subject = last.subject if last and last.subject else build_subject(company)
    msg.subject = base_subject if base_subject.lower().startswith("re:") else f"Re: {base_subject}"
    msg.body_text = body
    msg.body_html = None
    msg.tracking_code = company.tracking_code
    msg.template_name = "followup"
    msg.llm_paragraph = None
    msg.error = None
    msg.in_reply_to = None
    msg.references_header = None
    if last and last.message_id:
        msg.in_reply_to = last.message_id
        refs = (last.references_header or "").split()
        if last.message_id not in refs:
            refs.append(last.message_id)
        msg.references_header = " ".join(refs)
    session.flush()
    services.add_event(session, company, EventType.manual, f"Hatırlatma taslağı {'üretildi' if is_new else 'yeniden üretildi'}", actor=Actor.system, email_message=msg)
    session.flush()
    log.info("Hatırlatma taslağı hazır [%s] %s -> %s", company.id, company.name, msg.to_addr)
    return ComposeResult(ok=True, message=msg, template_name="followup")


_WHY_MARKER = "@@WHY_PARAGRAPH@@"


def static_paragraph(template_name: str, company: Company, profile: str = "") -> str:
    """Şablonun LLM yerine kullandığı sabit 'neden bu şirket' paragrafı (gövdede bulup değiştirmek için)."""
    marked = render(template_name, **_context(company, why_paragraph=_WHY_MARKER, profile=profile)).split("\n\n")
    plain = render(template_name, **_context(company, why_paragraph=None, profile=profile)).split("\n\n")
    for i, par in enumerate(marked):
        if _WHY_MARKER in par and i < len(plain):
            return plain[i].strip()
    return ""


def regenerate_paragraph(msg: EmailMessage, session: Session, *, use_llm: bool = True, text: str | None = None) -> ComposeResult:
    """Taslaktaki 'neden bu şirket' paragrafını yeniler; gövdenin geri kalanını (kullanıcı düzenlemeleri) korur.

    Önceki LLM paragrafı ya da şablonun sabit paragrafı gövdede aynen bulunursa yalnızca o değiştirilir.
    Bulunamazsa (kullanıcı paragrafı elle değiştirmiştir) yeni paragraf giriş paragrafından sonra eklenir
    ve ``note`` ile bildirilir. Konu ve diğer alanlar değişmez.
    """
    company = msg.company
    if company is None:
        return ComposeResult(ok=False, error="Mesajın şirketi yok.")
    if msg.direction != Direction.outbound or msg.status != MailStatus.draft:
        return ComposeResult(ok=False, error="Yalnızca taslakların paragrafı yeniden üretilir.")
    if is_followup(msg):
        return ComposeResult(ok=False, error="Hatırlatma mailinde LLM paragrafı yok; taslağı bütünüyle yeniden üretin.")
    err = _precheck(company)
    if err:
        return ComposeResult(ok=False, error=err)
    profile = llm.load_profile(required=False)
    if text is not None:  # dışarıda (elle ya da başka bir oturumda) yazılmış paragraf
        why, used_llm = (llm.clean_paragraph(text) or None), True
    else:
        why, used_llm = _why_paragraph(company, profile, use_llm=use_llm)
    template_name = msg.template_name if msg.template_name in TEMPLATE_NAMES else pick_template(company)
    fallback = static_paragraph(template_name, company, profile)
    new_par = (why or fallback).strip()
    body = (msg.body_text or "").replace("\r\n", "\n")
    note = None
    for old in ((msg.llm_paragraph or "").strip(), fallback):
        if old and old in body:
            body = body.replace(old, new_par, 1)
            break
    else:
        paragraphs = [p for p in body.strip().split("\n\n")] if body.strip() else []
        insert_at = min(2, len(paragraphs))
        paragraphs.insert(insert_at, new_par)
        body = "\n\n".join(paragraphs)
        note = "Önceki paragraf gövdede bulunamadı (elle değiştirilmiş); yeni paragraf giriş bölümünden sonra eklendi, lütfen kontrol edin."
        log.info("Paragraf gövdede bulunamadı, eklendi [%s]", company.id)
    msg.body_text = body.strip() + "\n"
    msg.llm_paragraph = why
    msg.error = None
    session.flush()
    services.add_event(session, company, EventType.manual, "Paragraf yeniden üretildi" + (" (LLM)" if used_llm else " (şablon)"), actor=Actor.system, email_message=msg)
    session.flush()
    return ComposeResult(ok=True, message=msg, template_name=template_name, used_llm=used_llm, note=note)


def is_followup(msg: EmailMessage) -> bool:
    return (msg.template_name or "") == "followup"
