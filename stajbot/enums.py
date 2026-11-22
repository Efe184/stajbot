"""Projedeki tüm enum'lar ve hedef şehir tanımları (paylaşılan sözleşme)."""
from __future__ import annotations

import enum
import unicodedata


class ReviewStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    later = "later"


class AppStatus(str, enum.Enum):
    found = "found"
    ready = "ready"
    drafted = "drafted"
    queued = "queued"
    sent = "sent"
    bounced = "bounced"
    no_reply = "no_reply"
    followup_sent = "followup_sent"
    replied = "replied"
    positive = "positive"
    negative = "negative"
    info_requested = "info_requested"
    phone_call = "phone_call"
    interview = "interview"
    presentation = "presentation"
    offer = "offer"
    accepted = "accepted"
    closed = "closed"


APP_STATUS_LABELS: dict[AppStatus, str] = {
    AppStatus.found: "Bulundu",
    AppStatus.ready: "Hazır",
    AppStatus.drafted: "Taslak üretildi",
    AppStatus.queued: "Gönderim kuyruğunda",
    AppStatus.sent: "Gönderildi",
    AppStatus.bounced: "Mail ulaşmadı",
    AppStatus.no_reply: "Cevap yok",
    AppStatus.followup_sent: "Hatırlatma gönderildi",
    AppStatus.replied: "Cevap geldi",
    AppStatus.positive: "Olumlu",
    AppStatus.negative: "Olumsuz",
    AppStatus.info_requested: "Bilgi istediler",
    AppStatus.phone_call: "Telefon görüşmesi",
    AppStatus.interview: "Mülakat",
    AppStatus.presentation: "Sunum / teknik görüşme",
    AppStatus.offer: "Teklif",
    AppStatus.accepted: "Kabul ettim",
    AppStatus.closed: "Kapandı",
}

# Sistemin kendi yaptığı geçişler. Kullanıcı her geçişi yapabilir.
SYSTEM_TRANSITIONS: dict[AppStatus, set[AppStatus]] = {
    AppStatus.found: {AppStatus.ready},
    AppStatus.ready: {AppStatus.drafted},
    AppStatus.drafted: {AppStatus.queued},
    AppStatus.queued: {AppStatus.sent},
    AppStatus.sent: {AppStatus.replied, AppStatus.no_reply, AppStatus.bounced},
    AppStatus.no_reply: {AppStatus.followup_sent, AppStatus.replied, AppStatus.bounced},
    AppStatus.followup_sent: {AppStatus.replied, AppStatus.bounced},
    AppStatus.bounced: {AppStatus.replied},
}

# Bu durumlardaki şirketlere asla otomatik mail gitmez.
NO_MAIL_STATUSES: set[AppStatus] = {AppStatus.accepted, AppStatus.closed, AppStatus.negative}


class SizeBucket(str, enum.Enum):
    unknown = "unknown"
    s1_10 = "s1_10"
    s11_50 = "s11_50"
    s51_200 = "s51_200"
    s201_1000 = "s201_1000"
    s1000_plus = "s1000_plus"


SIZE_LABELS: dict[SizeBucket, str] = {
    SizeBucket.unknown: "Bilinmiyor",
    SizeBucket.s1_10: "1-10",
    SizeBucket.s11_50: "11-50",
    SizeBucket.s51_200: "51-200",
    SizeBucket.s201_1000: "201-1000",
    SizeBucket.s1000_plus: "1000+",
}


def size_bucket_for(count: int | None) -> SizeBucket:
    """Çalışan sayısından büyüklük aralığı üretir."""
    if count is None or count <= 0:
        return SizeBucket.unknown
    if count <= 10:
        return SizeBucket.s1_10
    if count <= 50:
        return SizeBucket.s11_50
    if count <= 200:
        return SizeBucket.s51_200
    if count <= 1000:
        return SizeBucket.s201_1000
    return SizeBucket.s1000_plus


class ContactKind(str, enum.Enum):
    hr = "hr"            # ik@, kariyer@, hr@, staj@
    personal = "personal"  # ad.soyad@
    info = "info"        # info@, iletisim@
    other = "other"


CONTACT_KIND_PRIORITY: dict[ContactKind, int] = {
    ContactKind.hr: 1,
    ContactKind.personal: 2,
    ContactKind.info: 3,
    ContactKind.other: 4,
}


class EventType(str, enum.Enum):
    status_change = "status_change"
    email_sent = "email_sent"
    email_received = "email_received"
    note = "note"
    call = "call"
    interview = "interview"
    presentation = "presentation"
    offer = "offer"
    enrich = "enrich"
    collect = "collect"
    manual = "manual"


class Actor(str, enum.Enum):
    system = "system"
    user = "user"


class Direction(str, enum.Enum):
    outbound = "outbound"
    inbound = "inbound"


class MailStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    queued = "queued"
    sent = "sent"
    failed = "failed"
    received = "received"


class MatchMethod(str, enum.Enum):
    domain = "domain"
    message_id = "message_id"
    tracking_code = "tracking_code"
    manual = "manual"
    none = "none"


class Sentiment(str, enum.Enum):
    positive = "positive"
    negative = "negative"
    info = "info"
    unknown = "unknown"


# --- Şehirler -----------------------------------------------------------------

TARGET_CITIES: list[str] = [
    "Konya",
    "Ankara",
    "İstanbul",
    "İzmir",
    "Eskişehir",
    "Bursa",
    "Sakarya",
    "Antalya",
    "Mersin",
    "Afyonkarahisar",
]

# Yaygın yazım farklılıkları -> kanonik ad
_CITY_ALIASES: dict[str, str] = {
    "istanbul": "İstanbul",
    "izmir": "İzmir",
    "eskisehir": "Eskişehir",
    "afyon": "Afyonkarahisar",
    "afyonkarahisar": "Afyonkarahisar",
    "icel": "Mersin",
    "mersin": "Mersin",
    "adapazari": "Sakarya",
    "sakarya": "Sakarya",
    "ankara": "Ankara",
    "konya": "Konya",
    "bursa": "Bursa",
    "antalya": "Antalya",
}

# Hedef şehirlerdeki bazı ilçe/bölge adları -> şehir (Google Places ve adres metinleri için)
_DISTRICT_TO_CITY: dict[str, str] = {
    "gebze": "Kocaeli",  # Bilişim Vadisi Gebze'de; hedef dışı ama doğru etiketlensin
    "kartal": "İstanbul", "maslak": "İstanbul", "sariyer": "İstanbul", "kadikoy": "İstanbul",
    "atasehir": "İstanbul", "umraniye": "İstanbul", "pendik": "İstanbul", "sisli": "İstanbul",
    "besiktas": "İstanbul", "levent": "İstanbul", "beykoz": "İstanbul", "tuzla": "İstanbul",
    "cankaya": "Ankara", "yenimahalle": "Ankara", "golbasi": "Ankara", "bilkent": "Ankara",
    "odtu": "Ankara", "etimesgut": "Ankara", "sincan": "Ankara", "kecioren": "Ankara",
    "bornova": "İzmir", "konak": "İzmir", "karsiyaka": "İzmir", "bayrakli": "İzmir", "urla": "İzmir",
    "nilufer": "Bursa", "osmangazi": "Bursa", "yildirim": "Bursa",
    "selcuklu": "Konya", "meram": "Konya", "karatay": "Konya",
    "tepebasi": "Eskişehir", "odunpazari": "Eskişehir",
    "serdivan": "Sakarya", "adapazari": "Sakarya",
    "muratpasa": "Antalya", "konyaalti": "Antalya", "kepez": "Antalya",
    "yenisehir": "Mersin", "mezitli": "Mersin", "toroslar": "Mersin", "tarsus": "Mersin",
}


def ascii_fold(text: str) -> str:
    """Türkçe karakterleri ASCII'ye indirger ve küçük harfe çevirir."""
    text = text.replace("İ", "i").replace("I", "i").replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def normalize_city(raw: str | None) -> str | None:
    """Ham şehir metnini kanonik şehir adına çevirir. Tanınmazsa baş harfi büyük ham metni döner."""
    if not raw:
        return None
    key = ascii_fold(raw)
    key = key.replace("/", " ").replace(",", " ").replace("-", " ")
    tokens = [t for t in key.split() if t]
    for t in tokens:
        if t in _CITY_ALIASES:
            return _CITY_ALIASES[t]
    for t in tokens:
        if t in _DISTRICT_TO_CITY:
            return _DISTRICT_TO_CITY[t]
    joined = " ".join(tokens)
    if joined in _CITY_ALIASES:
        return _CITY_ALIASES[joined]
    return raw.strip().title() if raw.strip() else None


def is_target_city(city: str | None, targets: list[str] | None = None) -> bool:
    """Şehir hedef listesinde mi?

    ``targets`` None ise ``TARGET_CITIES`` kullanılır. Açıkça verilen listedeki None/boş girdiler
    yok sayılır; liste bu temizlikten sonra boşsa hiçbir şehir hedef sayılmaz (varsayılana düşülmez).
    """
    if not city:
        return False
    if targets is None:
        targets = TARGET_CITIES
    folded = {ascii_fold(str(t)) for t in targets if t is not None and str(t).strip()}
    if not folded:
        return False
    return ascii_fold(str(city)) in folded
