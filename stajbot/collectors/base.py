"""Collector sözleşmesi: RawCompany ve Collector protokolü. Bkz. MIMARI.md bölüm 6."""
from __future__ import annotations

import functools
import ssl
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol

import httpx

from ..config import get_settings
from ..log import get_logger

log = get_logger(__name__)


@dataclass
class RawCompany:
    name: str
    source: str
    source_url: str | None = None
    website: str | None = None
    city: str | None = None
    district: str | None = None
    address: str | None = None
    phone: str | None = None
    sector: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    employee_count: int | None = None
    linkedin_url: str | None = None
    is_arge_center: bool = False
    is_teknopark: bool = False
    teknopark_name: str | None = None
    internship_posting_url: str | None = None
    school_protocol: bool = False
    raw: dict = field(default_factory=dict)


class Collector(Protocol):
    name: str

    def collect(self, cities: list[str] | None = None) -> Iterator[RawCompany]: ...


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext | bool:
    """İşletim sisteminin sertifika deposunu kullanan SSL bağlamı.

    Bazı Türk kurum siteleri (ör. Bilkent Cyberpark) ara sertifikayı göndermez; certifi ile doğrulama
    "unable to get local issuer certificate" hatası verir. Windows/macOS deposu eksik ara sertifikayı
    AIA ile tamamlayabildiği için truststore kullanılır. Paket yoksa httpx varsayılanına (certifi) düşülür.
    """
    try:
        import truststore
    except ImportError:
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def make_http_client(**kwargs) -> httpx.Client:
    """Projede standart HTTP istemcisi: UA, zaman aşımı, yönlendirme takibi, sistem sertifika deposu."""
    s = get_settings()
    headers = {"User-Agent": s.http_user_agent, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7"}
    headers.update(kwargs.pop("headers", {}))
    kwargs.setdefault("verify", _ssl_context())
    return httpx.Client(timeout=s.http_timeout_sec, follow_redirects=True, headers=headers, **kwargs)


class Throttle:
    """İstekler arasında en az `delay` saniye bekletir."""

    def __init__(self, delay: float | None = None):
        self.delay = get_settings().crawl_delay_sec if delay is None else delay
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        rem = self.delay - (now - self._last)
        if rem > 0:
            time.sleep(rem)
        self._last = time.monotonic()


def safe_get(client: httpx.Client, url: str, throttle: Throttle | None = None, **kw) -> httpx.Response | None:
    """GET; hata olursa loglar ve None döner."""
    if throttle:
        throttle.wait()
    try:
        r = client.get(url, **kw)
        if r.status_code >= 400:
            log.warning("HTTP %s: %s", r.status_code, url)
            return None
        return r
    except httpx.HTTPError as e:
        log.warning("HTTP hatası %s: %s", url, e)
        return None
