"""Otomatik puan (0-100), deterministik. Ağırlıklar MIMARI.md bölüm 7.

- hedef şehir zorunlu (değilse 0)
- kaynak sayısı >= 2: +10
- staj ilanı: +25
- Ar-Ge merkezi: +15
- teknopark: +10
- okul protokolü: +25
- HR e-postası: +15; yalnızca başka tür e-posta (info/kişisel/diğer): +5
- tech_stack ∩ profil teknolojileri: her biri +5, en çok +20
- büyüklük 11-1000 arası: +5
"""
from __future__ import annotations

from pathlib import Path

from ..config import get_settings
from ..enums import ContactKind, SizeBucket, is_target_city, normalize_city
from ..log import get_logger
from ..models import Company
from . import keywords

log = get_logger(__name__)

WEIGHTS: dict[str, int] = {
    "multi_source": 10,
    "internship_posting": 25,
    "arge_center": 15,
    "teknopark": 10,
    "school_protocol": 25,
    "hr_email": 15,
    "other_email": 5,
    "tech_each": 5,
    "tech_max": 20,
    "size": 5,
}

_MID_SIZES = {SizeBucket.s11_50, SizeBucket.s51_200, SizeBucket.s201_1000}


def profile_techs(path: Path | None = None) -> list[str]:
    """``data/profil.md`` içindeki teknolojileri çıkarır; dosya yoksa boş liste."""
    p = path or get_settings().abs_profile_path
    try:
        text = Path(p).read_text(encoding="utf-8")
    except FileNotFoundError:
        log.debug("Profil dosyası yok: %s", p)
        return []
    except OSError as e:
        log.warning("Profil dosyası okunamadı (%s): %s", p, e)
        return []
    return keywords.profile_technologies(text)


def _fold_set(items) -> set[str]:
    return {keywords.fold(i) for i in (items or []) if i}


def _breakdown(company: Company, profile: list[str] | None) -> list[tuple[str, int]]:
    """(açıklama, puan) listesi; hedef şehir dışıysa tek bir sıfır satırı."""
    settings = get_settings()
    # Şehir normalde ingest'te normalize edilir; elle girilen "Afyon", "ISTANBUL" gibi yazımlar da tanınsın
    if not is_target_city(normalize_city(company.city), list(settings.target_cities)):
        return [(f"Hedef şehir dışı ({company.city or 'şehir yok'}): 0 puan", 0)]
    rows: list[tuple[str, int]] = []
    n_sources = len({s.source for s in (company.sources or [])})
    if n_sources >= 2:
        rows.append((f"{n_sources} farklı kaynakta geçiyor", WEIGHTS["multi_source"]))
    if company.has_internship_posting:
        rows.append(("Staj ilanı var", WEIGHTS["internship_posting"]))
    if company.is_arge_center:
        rows.append(("Ar-Ge / Tasarım merkezi", WEIGHTS["arge_center"]))
    if company.is_teknopark:
        rows.append(("Teknopark firması", WEIGHTS["teknopark"]))
    if company.school_protocol:
        rows.append(("Okulla protokolü var", WEIGHTS["school_protocol"]))
    kinds = {c.kind for c in (company.contacts or [])}
    if ContactKind.hr in kinds:
        rows.append(("İK / kariyer e-postası var", WEIGHTS["hr_email"]))
    elif kinds:
        rows.append(("Yalnızca genel e-posta var", WEIGHTS["other_email"]))
    if profile is None:
        profile = profile_techs()
    common = sorted(_fold_set(company.tech_stack) & _fold_set(profile))
    if common:
        pts = min(len(common) * WEIGHTS["tech_each"], WEIGHTS["tech_max"])
        names = [t for t in (company.tech_stack or []) if keywords.fold(t) in common]
        rows.append((f"Profille örtüşen teknolojiler: {', '.join(names)}", pts))
    if company.size_bucket in _MID_SIZES:
        rows.append(("Büyüklük 11-1000 çalışan", WEIGHTS["size"]))
    return rows


def compute_score(company: Company, profile_techs: list[str] | None = None) -> int:
    total = sum(p for _, p in _breakdown(company, profile_techs))
    return max(0, min(100, int(total)))


def explain(company: Company, profile_techs: list[str] | None = None) -> list[str]:
    """Puanın nasıl oluştuğunu Türkçe satırlar halinde açıklar."""
    rows = _breakdown(company, profile_techs)
    out = [f"{label}: +{pts}" if pts else label for label, pts in rows]
    out.append(f"Toplam: {compute_score(company, profile_techs)}")
    return out
