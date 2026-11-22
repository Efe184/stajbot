"""RawCompany -> Company: normalize, tekilleştir, kaydet. Bkz. MIMARI.md bölüm 6.

Eşleştirme sırası: (1) web sitesi / e-posta alan adı (``Company.domain``), (2) ``(normalize_name(ad), şehir)``.
Var olan şirkette yalnızca boş alanlar doldurulur; bool bayraklar OR'lanır; etiketler birleştirilir;
``CompanySource`` ve ``Contact`` kayıtları eklenir. Yeni şirket ``found`` / ``pending`` olur ve bir
``Event(collect)`` yazılır. Collector'lar veritabanına yazmaz; tek yazma noktası burasıdır.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import services
from ..config import get_settings
from ..enums import (
    Actor,
    AppStatus,
    ContactKind,
    EventType,
    ReviewStatus,
    ascii_fold,
    is_target_city,
    normalize_city,
    size_bucket_for,
)
from ..log import get_logger
from ..models import Company, CompanySource
from ..utils import GENERIC_EMAIL_DOMAINS, clean_text, domain_of, normalize_name, normalize_url, now_utc
from .base import RawCompany

log = get_logger(__name__)

# Bu alan adları şirketi tanımlamaz (sosyal ağ / genel sayfalar); domain anahtarı olarak kullanılmaz.
NON_COMPANY_DOMAINS: frozenset[str] = frozenset(
    {
        "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
        "wixsite.com", "blogspot.com", "wordpress.com", "google.com", "maps.google.com", "goo.gl",
        "sanayi.gov.tr", "yasad.org.tr", "tubisad.org.tr",
    }
)

# Var olan şirkette boşsa doldurulan düz alanlar: (RawCompany alanı, Company alanı, uzunluk sınırı)
_FILL_FIELDS: tuple[tuple[str, str, int | None], ...] = (
    ("district", "district", 64),
    ("address", "address", None),
    ("phone", "phone", 64),
    ("sector", "sector", 128),
    ("description", "description", None),
    ("linkedin_url", "linkedin_url", 512),
    ("teknopark_name", "teknopark_name", 128),
    ("internship_posting_url", "internship_posting_url", 512),
)

# "www.x.com, www.y.com" / "x.com; y.com" / "x.com www.y.com" gibi çoklu site hücreleri bu ayraçlarla bölünür.
_URL_SPLIT_RE = re.compile(r"[,\s;|]+")


@dataclass
class IngestReport:
    created: int = 0
    updated: int = 0
    skipped_city: int = 0
    skipped_invalid: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped_city + self.skipped_invalid + len(self.errors)

    def summary(self) -> str:
        return (
            f"yeni: {self.created}, güncellenen: {self.updated}, hedef dışı şehir: {self.skipped_city}, "
            f"geçersiz: {self.skipped_invalid}, hata: {len(self.errors)}"
        )


def json_safe(value: Any) -> Any:
    """JSON sütununa yazılabilir hale getirir (tarih, Path, set vb. -> str/list)."""
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return {"deger": str(value)}


def pick_website(value: Any) -> str | None:
    """Ham site metninden ilk geçerli URL'yi seçer (``"www.x.com, www.y.com"`` -> ``https://www.x.com``).

    Alan adı çıkarılamayan metinler (``"yok"``, ``"-"``, ``"N/A"``) için None döner.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidates = [text] + [p for p in _URL_SPLIT_RE.split(text) if p]
    for cand in candidates:
        url = normalize_url(cand)
        if url and domain_of(url) is not None:
            return url
    return None


def company_domain(raw: RawCompany) -> str | None:
    """Web sitesinden, yoksa (genel olmayan) e-posta alan adından tekilleştirme anahtarı üretir."""
    website = pick_website(raw.website)
    dom = domain_of(website) if website else None
    if dom and not _is_company_domain(dom):
        dom = None
    if dom:
        return dom
    for e in raw.emails or []:
        d = domain_of(str(e)) if e else None
        if d and _is_company_domain(d):
            return d
    return None


def _is_company_domain(dom: str) -> bool:
    if dom in GENERIC_EMAIL_DOMAINS:
        return False
    root = ".".join(dom.split(".")[-2:])
    return dom not in NON_COMPANY_DOMAINS and root not in NON_COMPANY_DOMAINS


def _city_key(city: str | None) -> str | None:
    """Şehir anahtarı: veritabanında normalize edilmemiş yazımlar ("ISTANBUL") da aynı anahtara iner."""
    if not city:
        return None
    norm = normalize_city(str(city))
    return ascii_fold(norm) if norm else None


def name_key(name: str | None, city: str | None) -> tuple[str, str | None]:
    return normalize_name(name), _city_key(city)


def _to_int(value: Any) -> int | None:
    """"120", "120+", "~500 çalışan" gibi değerlerden tam sayı çıkarır; çıkmazsa None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    m = re.search(r"\d[\d.]*", str(value).replace(",", ""))
    if not m:
        return None
    try:
        n = int(m.group(0).replace(".", ""))
    except ValueError:
        return None
    return n if n > 0 else None


class _Index:
    """Oturumdaki şirketler için domain ve (ad, şehir) haritaları; aynı parti içinde de tekilleştirir.

    Kaynaklar ve kişiler tek seferde (selectinload) yüklenir; böylece binlerce kayıtlık yeniden
    toplamada kayıt başına ek SELECT çalışmaz.
    """

    def __init__(self, session: Session):
        self.by_domain: dict[str, Company] = {}
        self.by_name: dict[tuple[str, str | None], Company] = {}
        self.by_name_only: dict[str, list[Company]] = {}
        stmt = select(Company).options(selectinload(Company.sources), selectinload(Company.contacts))
        for c in session.scalars(stmt):
            self.add(c)

    def add(self, c: Company) -> None:
        if c.domain:
            self.by_domain[c.domain] = c
        key = name_key(c.name, c.city)
        if key[0]:
            self.by_name.setdefault(key, c)
            bucket = self.by_name_only.setdefault(key[0], [])
            if not any(x is c for x in bucket):
                bucket.append(c)

    def find(self, dom: str | None, name: str, city: str | None) -> Company | None:
        if dom and dom in self.by_domain:
            return self.by_domain[dom]
        key = name_key(name, city)
        if not key[0]:
            return None
        if key in self.by_name:
            return self.by_name[key]
        # Şehri bilinmeyen taraf varsa (ham kayıt ya da veritabanındaki şirket) ve ad tekse eşle.
        bucket = self.by_name_only.get(key[0]) or []
        if len(bucket) == 1 and (key[1] is None or bucket[0].city is None):
            return bucket[0]
        return None


def _apply_source(session: Session, company: Company, raw: RawCompany) -> None:
    src = next((s for s in company.sources if s.source == raw.source), None)
    data = json_safe(raw.raw or {})
    if src is None:
        session.add(CompanySource(company=company, source=raw.source, source_url=raw.source_url, raw=data))
    else:
        src.source_url = raw.source_url or src.source_url
        if data:
            src.raw = data
        src.collected_at = now_utc()


def _apply_contacts(session: Session, company: Company, raw: RawCompany) -> int:
    n = 0
    contact_name = raw.raw.get("contact_name") if isinstance(raw.raw, dict) else None
    if not isinstance(contact_name, str) or not contact_name.strip():
        contact_name = None
    emails = [str(e).strip() for e in (raw.emails or []) if e]
    for e in emails:
        # Kişi adı tek e-posta varsa ona, birden çok varsa yalnızca kişisel görünümlü (ad.soyad@) olana yazılır.
        name = None
        if contact_name and (len(emails) == 1 or services.classify_contact_kind(e.lower()) == ContactKind.personal):
            name = contact_name
        if services.add_contact(session, company, e, name=name, source=raw.source):
            n += 1
    return n


def _merge_tags(company: Company, raw: RawCompany) -> None:
    tags = list(company.tags or [])
    for t in raw.tags or []:
        t = clean_text(str(t), 64)
        if t and t not in tags:
            tags.append(t)
    if tags != (company.tags or []):
        company.tags = tags


def _fill_existing(session: Session, company: Company, raw: RawCompany, *, city: str | None, website: str | None, dom: str | None) -> None:
    if website and not company.website:
        company.website = website[:512]
    if dom and not company.domain and dom not in _domain_taken(session, company, dom):
        company.domain = dom
    if city and not company.city:
        company.city = city
    for src_f, dst_f, limit in _FILL_FIELDS:
        val = getattr(raw, src_f, None)
        if val and not getattr(company, dst_f):
            setattr(company, dst_f, clean_text(str(val), limit) if limit else clean_text(str(val)))
    count = _to_int(raw.employee_count)
    if count and not company.employee_count:
        company.employee_count = count
        company.size_bucket = size_bucket_for(count)
    company.is_arge_center = bool(company.is_arge_center or raw.is_arge_center)
    company.is_teknopark = bool(company.is_teknopark or raw.is_teknopark)
    company.school_protocol = bool(company.school_protocol or raw.school_protocol)
    if raw.internship_posting_url:
        company.has_internship_posting = True
    _merge_tags(company, raw)
    company.updated_at = now_utc()


def _domain_taken(session: Session, company: Company, dom: str) -> set[str]:
    other = session.scalar(select(Company.id).where(Company.domain == dom, Company.id != company.id))
    return {dom} if other is not None else set()


def _create(session: Session, raw: RawCompany, *, name: str, city: str | None, website: str | None, dom: str | None) -> Company:
    count = _to_int(raw.employee_count)
    c = Company(
        name=name,
        domain=dom,
        website=website[:512] if website else None,
        city=city,
        district=clean_text(raw.district, 64),
        address=clean_text(raw.address),
        phone=clean_text(raw.phone, 64),
        sector=clean_text(raw.sector, 128),
        description=clean_text(raw.description),
        tags=[],
        tech_stack=[],
        employee_count=count,
        size_bucket=size_bucket_for(count),
        linkedin_url=clean_text(raw.linkedin_url, 512),
        has_internship_posting=bool(raw.internship_posting_url),
        internship_posting_url=clean_text(raw.internship_posting_url, 512),
        is_arge_center=bool(raw.is_arge_center),
        is_teknopark=bool(raw.is_teknopark),
        teknopark_name=clean_text(raw.teknopark_name, 128),
        school_protocol=bool(raw.school_protocol),
        status=AppStatus.found,
        review_status=ReviewStatus.pending,
    )
    notes = raw.raw.get("notes") if isinstance(raw.raw, dict) else None
    if isinstance(notes, str) and notes.strip():
        c.notes = notes.strip()
    _merge_tags(c, raw)
    session.add(c)
    session.flush()
    services.add_event(
        session, c, EventType.collect, f"Toplandı: {raw.source}",
        body=raw.source_url, actor=Actor.system,
    )
    return c


def ingest_one(raw: RawCompany, session: Session, index: _Index, report: IngestReport, *, allow_non_target: bool, targets: list[str]) -> Company | None:
    name = clean_text(raw.name, 255)
    if not name:
        report.skipped_invalid += 1
        return None
    city = normalize_city(clean_text(raw.city)) if raw.city is not None else None
    if not allow_non_target and not is_target_city(city, targets):
        report.skipped_city += 1
        log.debug("Hedef dışı şehir atlandı: %s (%s)", name, city)
        return None
    website = pick_website(raw.website)
    dom = company_domain(raw)
    existing = index.find(dom, name, city)
    if existing is not None:
        _fill_existing(session, existing, raw, city=city, website=website, dom=dom)
        _apply_source(session, existing, raw)
        _apply_contacts(session, existing, raw)
        session.flush()
        index.add(existing)
        report.updated += 1
        return existing
    c = _create(session, raw, name=name, city=city, website=website, dom=dom)
    _apply_source(session, c, raw)
    _apply_contacts(session, c, raw)
    session.flush()
    index.add(c)
    report.created += 1
    return c


def ingest(raws: Iterable[RawCompany], session: Session, *, allow_non_target: bool = False) -> IngestReport:
    """RawCompany akışını veritabanına işler. Hiç exception fırlatmaz; hatalar raporda listelenir."""
    report = IngestReport()
    targets = list(get_settings().target_cities)
    try:
        index = _Index(session)
    except Exception as e:  # noqa: BLE001
        log.error("Şirket dizini okunamadı: %s", e)
        report.errors.append(f"Şirket dizini okunamadı: {e}")
        return report
    it = iter(raws) if raws is not None else iter(())
    while True:
        try:
            raw = next(it)
        except StopIteration:
            break
        except Exception as e:  # noqa: BLE001 - toplayıcı akışı patlarsa o ana kadar işlenenler korunur
            msg = f"Kaynak akışı kesildi: {type(e).__name__}: {e}"
            log.warning(msg, exc_info=log.isEnabledFor(10))
            report.errors.append(msg)
            break
        if not isinstance(raw, RawCompany):
            report.skipped_invalid += 1
            continue
        try:
            with session.begin_nested():
                ingest_one(raw, session, index, report, allow_non_target=allow_non_target, targets=targets)
        except Exception as e:  # noqa: BLE001 - tek kayıt düşerse akış durmaz
            msg = f"{raw.source}: '{clean_text(raw.name, 60) or '?'}' kaydedilemedi: {type(e).__name__}: {e}"
            log.warning(msg, exc_info=log.isEnabledFor(10))
            report.errors.append(msg)
            # dizin bozulmuş olabilir; yeniden kur
            try:
                index = _Index(session)
            except Exception:  # noqa: BLE001
                pass
    log.info("Ingest bitti — %s", report.summary())
    return report
