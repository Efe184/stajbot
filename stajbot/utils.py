"""Küçük yardımcılar (paylaşılan sözleşme)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from .enums import ascii_fold

GENERIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com",
        "yandex.com", "yandex.com.tr", "icloud.com", "mynet.com", "windowslive.com", "msn.com",
        "protonmail.com", "proton.me", "superonline.com", "ttmail.com",
    }
)

_LEGAL_SUFFIXES = (
    "anonim", "sirketi", "limited", "a.s.", "a.s", "as.", "ltd. sti.", "ltd.sti.", "ltd sti",
    "ltd.", "ltd", "sti.", "sti", "san.", "san", "tic.", "tic", "ve", "sanayi", "ticaret",
    "teknoloji", "bilisim", "yazilim", "muhendislik", "danismanlik", "hizmetleri", "hizmetler",
    "arge", "ar-ge", "inc", "inc.", "llc", "gmbh", "co.", "co",
)


def now_utc() -> datetime:
    """Zaman damgası (timezone bilgisi olmadan, UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def domain_of(url_or_email: str | None) -> str | None:
    """URL ya da e-postadan normalize alan adı üretir: küçük harf, www yok, port yok."""
    if not url_or_email:
        return None
    s = url_or_email.strip().lower()
    if "@" in s and " " not in s and not s.startswith(("http://", "https://")):
        s = s.rsplit("@", 1)[-1]
    else:
        if not s.startswith(("http://", "https://")):
            s = "http://" + s
        try:
            s = urlparse(s).hostname or ""
        except ValueError:
            return None
    s = s.split(":", 1)[0].strip(".")
    if s.startswith("www."):
        s = s[4:]
    if "." not in s or " " in s:
        return None
    return s or None


_SCHEME_RE = re.compile(r"^(?:https?://\s*)+", re.I)
_OTHER_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def normalize_url(url: str | None) -> str | None:
    """Şema eksikse https ekler, boşlukları temizler.

    http(s) dışı şemalar (``mailto:``, ``javascript:``, ``tel:``, ``ftp:``, ``//host``) için None döner;
    ``http://https://x`` gibi çift şema yazımını düzeltir (son şema korunur).
    """
    if not url:
        return None
    u = str(url).strip().strip("\"'").strip()
    if not u:
        return None
    m = _SCHEME_RE.match(u)
    if m:
        schemes = re.findall(r"https?://", m.group(0), re.I)
        u = schemes[-1].lower() + u[m.end():].lstrip()
        if not u.split("://", 1)[1]:
            return None
        return u
    if u.startswith("//") or _OTHER_SCHEME_RE.match(u):
        return None
    return "https://" + u


def normalize_name(name: str | None) -> str:
    """Şirket adını eşleştirme için sadeleştirir: ASCII, küçük harf, hukuki ekler ve noktalama yok."""
    if not name:
        return ""
    s = ascii_fold(name)
    # çok kelimeli hukuki ekler ("anonim şirketi", "limited şirketi") tek tek kelimeler silinmeden önce gider
    s = re.sub(r"\b(anonim|limited)\s+sirketi\b", " ", s)
    s = re.sub(r"[^a-z0-9\s.&-]", " ", s)
    s = s.replace("&", " ve ")
    tokens = [t for t in re.split(r"\s+", s) if t]
    cleaned = [t for t in tokens if t not in _LEGAL_SUFFIXES]
    if not cleaned:  # her şey silinirse orijinal tokenleri koru
        cleaned = tokens
    return " ".join(cleaned).strip(" .-")


def slugify(text: str) -> str:
    s = ascii_fold(text)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "x"


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def is_valid_email(email: str | None) -> bool:
    return bool(email) and bool(_EMAIL_RE.fullmatch(email.strip()))


def clean_text(text: str | None, limit: int | None = None) -> str | None:
    if text is None:
        return None
    t = re.sub(r"\s+", " ", str(text)).strip()
    if not t:
        return None
    if limit and len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t
