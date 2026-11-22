"""Sanayi ve Teknoloji Bakanlığı Ar-Ge Merkezleri / Tasarım Merkezleri listesi toplayıcısı ("argem").

Kaynak: Bakanlığın yayımladığı tek sayfalık XLSX dosyaları (firma adı, il, sektör, belge tarihi).
Resmî adresler sabittir ve dosya yerinde güncellenir:

* Ar-Ge Merkezleri:   https://www.sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx
* Tasarım Merkezleri: https://www.sanayi.gov.tr/assets/doc/Tasarımmerkeziistatistik.xlsx

Önemli kısıt: sanayi.gov.tr tarayıcı dışı istemcilere (httpx dahil) HTTP 200 ile küçük bir
"Request Rejected" HTML sayfası döndürür. Bu yüzden modül DOSYA TABANLI çalışır:

1. ``ArgemCollector(path=...)`` ile verilen dosya/dizin (çevrimdışı, elle indirilen dosya),
2. ``data/argem/*.xlsx`` (kullanıcı tarayıcıyla indirir, birkaç ayda bir yeniler),
3. ``data/cache/argem/`` içindeki taze önbellek,
4. ağ: önce doğrudan indirme denenir (proje UA'sı ile, tarayıcı taklidi YAPILMAZ), başarısızsa
   Wayback Machine'deki en yeni gerçek kopya (CDX API, mimetype=spreadsheet) indirilir.
   İndirilen her içerik ZIP imzası (``PK``) ile doğrulanır.

Sütunlar ADA GÖRE eşlenir (sıra dosya sürümüne göre değişir): 2020 dosyasında
``SIRA NO | ADI | SEKTÖR | İL | TARİH``, 2026 dosyasında ``SIRA NO | ADI | İL | SEKTÖR | TARİH``.

Çevrimdışı kullanım için ``parse_xlsx(bytes_or_path)`` ve ``parse_xlsx_records(...)``
fonksiyonları dışa aktarılır; testler ``tests/fixtures/argem/`` fikstürleriyle çalışır.
"""
from __future__ import annotations

import io
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import httpx
import openpyxl

from ..config import get_settings
from ..enums import ascii_fold, is_target_city, normalize_city
from ..log import get_logger
from ..utils import clean_text, normalize_name
from .base import RawCompany, Throttle, make_http_client

log = get_logger(__name__)

__all__ = [
    "SOURCE_NAME",
    "KIND_ARGE",
    "KIND_TASARIM",
    "KIND_TAGS",
    "OFFICIAL_URLS",
    "OFFICIAL_FILE_NAMES",
    "STATS_PAGE_URL",
    "ArgemRecord",
    "ColumnMap",
    "ArgemCollector",
    "collect",
    "parse_xlsx",
    "parse_xlsx_records",
    "iter_xlsx_records",
    "detect_columns",
    "detect_kind",
    "fold_header",
    "clean_company_name",
    "canonical_sector",
    "is_tech_sector",
    "is_tech_record",
    "dedupe_records",
    "match_key_name",
    "filter_records_by_cities",
    "record_to_raw",
    "is_xlsx_bytes",
    "download_direct",
    "wayback_latest_capture",
    "download_wayback",
    "fetch_xlsx",
    "cache_meta",
    "default_data_dir",
    "default_cache_dir",
    "local_xlsx_files",
    "manual_download_instructions",
]

# --- sabitler ------------------------------------------------------------------

SOURCE_NAME = "argem"

KIND_ARGE = "Ar-Ge Merkezi"
KIND_TASARIM = "Tasarım Merkezi"
KIND_TAGS: dict[str, str] = {KIND_ARGE: "arge-merkezi", KIND_TASARIM: "tasarim-merkezi"}

OFFICIAL_URLS: dict[str, str] = {
    KIND_ARGE: "https://www.sanayi.gov.tr/assets/doc/Ar-Gemerkezleri.xlsx",
    KIND_TASARIM: "https://www.sanayi.gov.tr/assets/doc/Tasar%C4%B1mmerkeziistatistik.xlsx",
}
# Önbellek / elle indirme için dosya adları
OFFICIAL_FILE_NAMES: dict[str, str] = {
    KIND_ARGE: "Ar-Gemerkezleri.xlsx",
    KIND_TASARIM: "Tasarimmerkeziistatistik.xlsx",
}
STATS_PAGE_URL = "https://www.sanayi.gov.tr/istatistikler/istatistiki-bilgiler/mi0203011501"

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_DOWNLOAD_URL = "https://web.archive.org/web/{timestamp}id_/{original}"
WAYBACK_RETRIES = 4
WAYBACK_RETRY_SLEEP_SEC = 6.0

DEFAULT_MAX_AGE_DAYS = 90
MIN_XLSX_BYTES = 2000  # WAF stub ~244 bayt; gerçek dosya on binlerce bayt
HEADER_SCAN_ROWS = 15

# Sektör etiketleri sürümler arasında kaydı; kanonik ada katla (anahtar: fold edilmiş metin)
_SECTOR_SYNONYMS: dict[str, str] = {
    "ELEKTRIK-ELEKTRONIK": "Elektronik",
    "ELEKTRIK ELEKTRONIK": "Elektronik",
    "GIDA": "Gıda Sanayi",
    "PLASTIK-KAUCUK": "Lastik-Plastik-Kauçuk",
    "OTOMOTIV YAN SANAYI": "Otomotiv Yan Sanayi",
    "IMALAT SANAYI": "İmalat Sanayi",
    "DERI TEKNOLOJILERI": "Deri ve Deri Ürünleri",
}

# Bilgisayar mühendisliği stajı için çekirdek sektörler (fold edilmiş)
TECH_SECTORS_CORE: frozenset[str] = frozenset(
    {
        "YAZILIM",
        "BILGISAYAR VE ILETISIM TEKNOLOJILERI",
        "ELEKTRONIK",
        "ELEKTRIK-ELEKTRONIK",
        "SAVUNMA SANAYI",
        "TELEKOMUNIKASYON",
        "BANKACILIK VE FINANS",
        "OTOMOTIV",
        "OTOMOTIV YAN SANAYI",
        "OTOMOTIV TASARIMI VE MUHENDISLIK",
        "HAVACILIK",
        "MEDYA VE ILETISIM",
    }
)
# Geniş küme (küçük şehirlerde havuzu büyütmek için, ``wide_sectors=True``)
TECH_SECTORS_WIDE: frozenset[str] = frozenset(
    {
        "MAKINE VE TECHIZAT IMALATI",
        "ENERJI",
        "MUHENDISLIK/MIMARLIK FAALIYETLERI",
        "DAYANIKLI TUKETIM MALLARI",
        "ULASTIRMA VE LOJISTIK",
        "SAGLIK",
        "MEDIKAL",
        "IKLIMLENDIRME",
    }
)
# Bilinmeyen sektör etiketleri (ör. Tasarım dosyası) için anahtar kelimeler (fold edilmiş, küçük harf)
_SECTOR_KEYWORDS_CORE = (
    "yazilim", "bilgisayar", "bilisim", "iletisim", "elektronik", "savunma", "telekom",
    "otomotiv", "havacilik", "finans", "bankacilik", "medya", "software", "robotik", "otomasyon",
)
_SECTOR_KEYWORDS_WIDE = (
    "makine", "enerji", "muhendislik", "dayanikli", "ulastirma", "lojistik", "saglik", "medikal",
    "iklimlendirme",
)
# Firma adında geçen teknoloji ipuçları (sektörü farklı olsa da ilgili olabilir)
NAME_TECH_KEYWORDS = (
    "yazilim", "bilisim", "software", "elektronik", "bilgi sistem", "bilgi teknoloji",
    "telekom", "robotik", "otomasyon", "siber", "savunma",
)

_PAREN_SUFFIX_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
_WS_RE = re.compile(r"\s+")


# --- veri yapıları ----------------------------------------------------------------

@dataclass
class ColumnMap:
    """Başlık satırından çıkarılan sütun indeksleri (0 tabanlı)."""

    name: int
    city: int
    sector: int | None = None
    date: int | None = None
    order: int | None = None
    header_row: int = 1  # 1 tabanlı satır numarası


@dataclass
class ArgemRecord:
    """XLSX'ten okunan tek satır (ham kayıt)."""

    name: str
    name_clean: str
    city: str | None
    sector: str | None
    sector_canonical: str | None
    cert_date: date | None
    kind: str
    sira_no: int | None = None
    site_note: str | None = None
    row_no: int | None = None
    source_url: str | None = None
    file_name: str | None = None
    file_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cert_date"] = self.cert_date.isoformat() if self.cert_date else None
        return d


# --- metin yardımcıları ----------------------------------------------------------

def _clean_cell(value: Any) -> str:
    """Hücre değerini metne çevirir; nbsp ve fazla boşlukları temizler."""
    if value is None:
        return ""
    s = str(value).replace("\xa0", " ").replace("​", "")
    return _WS_RE.sub(" ", s).strip()


def fold_header(value: Any) -> str:
    """Başlık metnini karşılaştırma için katlar: strip, büyük harf, Türkçe karakter -> ASCII."""
    s = _clean_cell(value)
    if not s:
        return ""
    return ascii_fold(s).upper()


def clean_company_name(name: str) -> str:
    """Addaki sondaki parantezli şube/yer notunu kaldırır ('... A.Ş. (Eskişehir)' -> '... A.Ş.')."""
    s = _clean_cell(name)
    m = _PAREN_SUFFIX_RE.search(s)
    if m:
        stripped = s[: m.start()].strip(" -–,")
        if stripped:
            s = stripped
    return s


def _site_note(name: str) -> str | None:
    m = _PAREN_SUFFIX_RE.search(_clean_cell(name))
    if m:
        note = m.group(1).strip()
        return note or None
    return None


def canonical_sector(sector: str | None) -> str | None:
    """Sürümler arası kayan sektör etiketlerini tek ada indirger."""
    s = _clean_cell(sector)
    if not s:
        return None
    return _SECTOR_SYNONYMS.get(fold_header(s), s)


def is_tech_sector(sector: str | None, *, wide: bool = False) -> bool:
    """Sektör metni bilgisayar mühendisliği stajı için ilgili mi? (yazılım/BT/elektronik/savunma/otomotiv/telekom)."""
    s = fold_header(sector)
    if not s:
        return False
    if s in TECH_SECTORS_CORE:
        return True
    if wide and s in TECH_SECTORS_WIDE:
        return True
    low = s.lower()
    if any(k in low for k in _SECTOR_KEYWORDS_CORE):
        return True
    if wide and any(k in low for k in _SECTOR_KEYWORDS_WIDE):
        return True
    return False


def is_tech_record(rec: ArgemRecord, *, wide: bool = False) -> bool:
    """Sektör ya da firma adı teknoloji ipucu taşıyorsa True."""
    if is_tech_sector(rec.sector, wide=wide):
        return True
    folded = ascii_fold(rec.name_clean or rec.name)
    return any(k in folded for k in NAME_TECH_KEYWORDS)


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Biçimlenmemiş Excel tarih seri numarası (ör. 44927 -> 2023-01-01)
        try:
            from openpyxl.utils.datetime import from_excel

            if 20000 <= float(value) <= 80000:  # 1954..2119 arası makul aralık
                d = from_excel(value)
                return d.date() if isinstance(d, datetime) else d
        except (ValueError, TypeError, OverflowError):
            return None
        return None
    s = _clean_cell(value)
    if not s:
        return None
    s = s.split("T", 1)[0] if "T" in s and "-" in s else s
    for fmt in (
        "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
        "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    s = _clean_cell(value)
    if s.isdigit():
        return int(s)
    return None


# --- başlık eşleme -------------------------------------------------------------------

def detect_columns(header: Sequence[Any], header_row: int = 1) -> ColumnMap | None:
    """Başlık hücrelerini ada göre sütunlara eşler. Ad ve il sütunu bulunamazsa None."""
    folded = [fold_header(h) for h in header]
    name_idx = city_idx = sector_idx = date_idx = order_idx = None
    for i, h in enumerate(folded):
        if not h:
            continue
        if name_idx is None and ("MERKEZ" in h and "ADI" in h):
            name_idx = i
        elif city_idx is None and h in ("IL", "ILI", "SEHIR", "IL ADI"):
            city_idx = i
        elif sector_idx is None and (h.startswith("SEKT") or "FAALIYET" in h):
            sector_idx = i
        elif date_idx is None and (h.startswith("BELGE") or "TARIH" in h):
            date_idx = i
        elif order_idx is None and (h.startswith("SIRA") or h in ("NO", "S.NO")):
            order_idx = i
    if name_idx is None:  # yedek: ADI / UNVAN / FIRMA içeren ilk başlık
        for i, h in enumerate(folded):
            if h and i not in (city_idx, sector_idx, date_idx, order_idx) and (
                "ADI" in h or "UNVAN" in h or "FIRMA" in h or "KURULUS" in h
            ):
                name_idx = i
                break
    if name_idx is None or city_idx is None:
        return None
    return ColumnMap(name=name_idx, city=city_idx, sector=sector_idx, date=date_idx, order=order_idx, header_row=header_row)


def detect_kind(header: Sequence[Any], file_name: str | None = None) -> str:
    """Başlıktan (ya da dosya adından) liste türünü çıkarır."""
    joined = " ".join(fold_header(h) for h in header)
    if "TASARIM" in joined:
        return KIND_TASARIM
    if "AR-GE" in joined or "ARGE" in joined:
        return KIND_ARGE
    if file_name and "TASARIM" in fold_header(file_name):
        return KIND_TASARIM
    return KIND_ARGE


# --- ayrıştırma ------------------------------------------------------------------------

def _open_workbook(src: bytes | bytearray | str | Path):
    if isinstance(src, (bytes, bytearray)):
        return openpyxl.load_workbook(io.BytesIO(bytes(src)), read_only=True, data_only=True)
    return openpyxl.load_workbook(Path(src), read_only=True, data_only=True)


def _file_meta(src: bytes | bytearray | str | Path, file_date: str | None) -> tuple[str | None, str | None]:
    if isinstance(src, (bytes, bytearray)):
        return None, file_date
    p = Path(src)
    if file_date is None:
        try:
            file_date = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).date().isoformat()
        except OSError:
            file_date = None
    return p.name, file_date


def iter_xlsx_records(
    src: bytes | bytearray | str | Path,
    *,
    kind: str | None = None,
    source_url: str | None = None,
    file_date: str | None = None,
) -> Iterator[ArgemRecord]:
    """XLSX'i satır satır okur ve ArgemRecord üretir.

    Başlık her sayfanın ilk 15 satırında aranır; başlığı bulunan İLK sayfa okunur (bazı
    bakanlık dosyalarında ilk sayfa özet/istatistik olabilir). Başlık hiçbir sayfada yoksa
    uyarı loglanır ve hiçbir kayıt üretilmez (exception fırlatılmaz).
    """
    file_name, file_date = _file_meta(src, file_date)
    label = file_name or "bellek"
    wb = _open_workbook(src)
    try:
        sheets = list(wb.worksheets)
        if not sheets:
            log.warning("Ar-Ge listesi boş: çalışma kitabında sayfa yok (%s)", label)
            return
        for ws in sheets:
            cols: ColumnMap | None = None
            detected_kind = kind
            for row_no, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if cols is None:
                    if row_no > HEADER_SCAN_ROWS:
                        break  # bu sayfada başlık yok; sonraki sayfaya geç
                    cols = detect_columns(row, header_row=row_no)
                    if cols is not None:
                        if detected_kind is None:
                            detected_kind = detect_kind(row, file_name)
                        log.debug(
                            "Başlık satırı %d (%s): %r -> %s",
                            row_no, ws.title, [_clean_cell(c) for c in row], detected_kind,
                        )
                    continue
                if row is None or cols.name >= len(row) or cols.city >= len(row):
                    continue
                raw_name = _clean_cell(row[cols.name])
                if not raw_name:
                    continue
                city = _clean_cell(row[cols.city]) or None
                sira_no = _to_int(row[cols.order]) if cols.order is not None and cols.order < len(row) else None
                if cols.order is not None and sira_no is None and not city:
                    continue  # toplam/not satırı (sıra numarası ve il yok)
                sector = _clean_cell(row[cols.sector]) if cols.sector is not None and cols.sector < len(row) else ""
                cert = _to_date(row[cols.date]) if cols.date is not None and cols.date < len(row) else None
                k = detected_kind or KIND_ARGE
                yield ArgemRecord(
                    name=raw_name,
                    name_clean=clean_company_name(raw_name),
                    city=city,
                    sector=sector or None,
                    sector_canonical=canonical_sector(sector),
                    cert_date=cert,
                    kind=k,
                    sira_no=sira_no,
                    site_note=_site_note(raw_name),
                    row_no=row_no,
                    source_url=source_url or OFFICIAL_URLS.get(k),
                    file_name=file_name,
                    file_date=file_date,
                )
            if cols is not None:
                return  # başlığı olan sayfa okundu; diğer sayfalara bakılmaz
        log.warning(
            "Ar-Ge listesi boş ya da başlıksız (%s): hiçbir sayfanın ilk %d satırında ad/il sütunu yok",
            label, HEADER_SCAN_ROWS,
        )
    finally:
        wb.close()


def parse_xlsx_records(src: bytes | bytearray | str | Path, **kw) -> list[ArgemRecord]:
    """Tüm satırları (filtre/tekilleştirme olmadan) ArgemRecord listesi olarak döner."""
    return list(iter_xlsx_records(src, **kw))


_LONG_LEGAL_SUFFIX_RE = re.compile(
    r"\b(anonim|limited)\s+sirketi\b|\bsanayi\s+ve\s+ticaret\b|\bticaret\s+ve\s+sanayi\b",
)


def match_key_name(name: str | None) -> str:
    """Tekilleştirme anahtarı: ``utils.normalize_name`` + çok kelimeli hukuki ekler ('Anonim Şirketi').

    ``normalize_name`` ekleri kelime kelime siler; 'A.Ş.' gider ama 'Anonim Şirketi' kalır.
    Bakanlık listesinde her iki yazım da geçtiği için burada önce bunlar temizlenir.
    """
    if not name:
        return ""
    folded = _LONG_LEGAL_SUFFIX_RE.sub(" ", ascii_fold(name))
    return normalize_name(folded)


def dedupe_records(records: Iterable[ArgemRecord]) -> list[ArgemRecord]:
    """Aynı firma + il için (şube notu farklı olsa da) tek kayıt bırakır; ilk görülen kalır."""
    seen: dict[tuple[str, str | None], ArgemRecord] = {}
    out: list[ArgemRecord] = []
    for r in records:
        key = (match_key_name(r.name_clean or r.name), normalize_city(r.city))
        if key in seen:
            first = seen[key]
            if r.site_note and first.site_note and r.site_note != first.site_note:
                # girdi kayıtlarını değiştirmemek için kopya üzerinde birleştir
                merged = replace(first, site_note=f"{first.site_note}; {r.site_note}")
                out[out.index(first)] = merged
                seen[key] = merged
            continue
        seen[key] = r
        out.append(r)
    return out


def filter_records_by_cities(records: Iterable[ArgemRecord], cities: Sequence[str] | None) -> list[ArgemRecord]:
    """Şehir filtresi: ``cities`` None/boşsa her şey döner."""
    if not cities:
        return list(records)
    # None / boş metin girdileri atılır; hepsi boşsa filtre yok sayılır
    targets = [normalize_city(c) or str(c).strip() for c in cities if c and str(c).strip()]
    if not targets:
        return list(records)
    return [r for r in records if r.city and is_target_city(normalize_city(r.city), targets)]


def record_to_raw(rec: ArgemRecord) -> RawCompany:
    """ArgemRecord -> RawCompany (ingest sözleşmesi)."""
    sector = rec.sector_canonical or rec.sector
    tag = KIND_TAGS.get(rec.kind, "arge-merkezi")
    parts = [f"Sanayi ve Teknoloji Bakanlığı {rec.kind} listesinde kayıtlı şirket"]
    if rec.cert_date:
        parts.append(f"belge tarihi {rec.cert_date.strftime('%d.%m.%Y')}")
    desc = ", ".join(parts) + "."
    if sector:
        desc += f" Sektör: {sector}."
    if rec.site_note:
        desc += f" Not: {rec.site_note}."
    raw = {
        "kaynak": SOURCE_NAME,
        "tur": rec.kind,
        "firma": rec.name,
        "firma_temiz": rec.name_clean,
        "il": rec.city,
        "sektor": rec.sector,
        "sektor_kanonik": rec.sector_canonical,
        "belge_tarihi": rec.cert_date.isoformat() if rec.cert_date else None,
        "sira_no": rec.sira_no,
        "sube_notu": rec.site_note,
        "dosya": rec.file_name,
        "dosya_tarihi": rec.file_date,
    }
    return RawCompany(
        name=rec.name_clean or rec.name,
        source=SOURCE_NAME,
        source_url=rec.source_url or OFFICIAL_URLS.get(rec.kind),
        city=normalize_city(rec.city),
        sector=clean_text(sector, 128),
        description=desc,
        tags=[tag],
        is_arge_center=True,
        raw=raw,
    )


def parse_xlsx(
    src: bytes | bytearray | str | Path,
    *,
    kind: str | None = None,
    source_url: str | None = None,
    file_date: str | None = None,
    cities: Sequence[str] | None = None,
    only_tech: bool = False,
    wide_sectors: bool = False,
    dedupe: bool = True,
) -> list[RawCompany]:
    """XLSX (bayt ya da yol) -> RawCompany listesi. Varsayılan: filtre yok, tekilleştirme var."""
    records = parse_xlsx_records(src, kind=kind, source_url=source_url, file_date=file_date)
    total = len(records)
    records = filter_records_by_cities(records, cities)
    if only_tech:
        records = [r for r in records if is_tech_record(r, wide=wide_sectors)]
    if dedupe:
        records = dedupe_records(records)
    log.info("Ar-Ge/Tasarım listesi ayrıştırıldı: %d satır, %d kayıt kaldı", total, len(records))
    return [record_to_raw(r) for r in records]


# --- indirme ---------------------------------------------------------------------------

def is_xlsx_bytes(data: bytes | None) -> bool:
    """ZIP imzası (PK) ve makul boyut: WAF 'Request Rejected' HTML sayfasını eler."""
    return bool(data) and data[:2] == b"PK" and len(data) >= MIN_XLSX_BYTES


def download_direct(url: str, client: httpx.Client, throttle: Throttle | None = None) -> bytes | None:
    """Resmî adresten doğrudan indirme dener. Tarayıcı taklidi yapılmaz; WAF engellerse None."""
    if throttle:
        throttle.wait()
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        log.warning("Doğrudan indirme başarısız (%s): %s", url, e)
        return None
    if r.status_code >= 400:
        log.warning("Doğrudan indirme HTTP %s: %s", r.status_code, url)
        return None
    if not is_xlsx_bytes(r.content):
        log.info("Doğrudan indirme XLSX değil (muhtemelen WAF engeli, %d bayt): %s", len(r.content), url)
        return None
    return r.content


def _cdx_target(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url)


def _parse_cdx_rows(rows: Any) -> tuple[str, str] | None:
    if not isinstance(rows, list):
        return None
    if rows and isinstance(rows[0], list) and rows[0] and rows[0][0] == "timestamp":
        rows = rows[1:]
    best: tuple[str, str] | None = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            continue
        ts, original, status, mime = str(row[0]), str(row[1]), str(row[2]), str(row[3])
        if status != "200" or "sheet" not in mime.lower():
            continue
        if best is None or ts > best[0]:
            best = (ts, original)
    return best


def wayback_latest_capture(
    url: str,
    client: httpx.Client,
    *,
    retries: int = WAYBACK_RETRIES,
    sleep_sec: float = WAYBACK_RETRY_SLEEP_SEC,
) -> tuple[str, str] | None:
    """Wayback CDX API'den en yeni gerçek (200 + spreadsheet mimetype) yakalamayı döner: (timestamp, original)."""
    params = {
        "url": _cdx_target(url),
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,length",
        "filter": ["statuscode:200", "mimetype:.*sheet.*"],
    }
    last_err: str | None = None
    for attempt in range(1, retries + 1):
        try:
            r = client.get(WAYBACK_CDX_URL, params=params)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            if r.status_code >= 400:
                log.warning("Wayback CDX HTTP %s: %s", r.status_code, url)
                return None
            text = r.text.strip()
            rows = json.loads(text) if text else []
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            last_err = str(e) or e.__class__.__name__
            if attempt < retries:
                log.info(
                    "Wayback CDX denemesi %d/%d başarısız (%s), %.0f sn sonra yeniden",
                    attempt, retries, last_err, sleep_sec,
                )
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
                continue
            log.warning("Wayback CDX erişilemedi: %s", last_err)
            return None
        return _parse_cdx_rows(rows)
    return None


def download_wayback(
    url: str,
    client: httpx.Client,
    *,
    throttle: Throttle | None = None,
    retries: int = WAYBACK_RETRIES,
    sleep_sec: float = WAYBACK_RETRY_SLEEP_SEC,
) -> tuple[bytes, str] | None:
    """Wayback'ten en yeni gerçek XLSX'i indirir: (içerik, yakalama zaman damgası)."""
    cap = wayback_latest_capture(url, client, retries=retries, sleep_sec=sleep_sec)
    if cap is None:
        log.warning("Wayback'te gerçek XLSX yakalaması bulunamadı: %s", url)
        return None
    ts, original = cap
    dl = WAYBACK_DOWNLOAD_URL.format(timestamp=ts, original=original)
    if throttle:
        throttle.wait()
    try:
        r = client.get(dl, follow_redirects=True)
    except httpx.HTTPError as e:
        log.warning("Wayback indirme başarısız (%s): %s", dl, e)
        return None
    if r.status_code >= 400:
        log.warning("Wayback indirme HTTP %s: %s", r.status_code, dl)
        return None
    if not is_xlsx_bytes(r.content):
        log.warning("Wayback yakalaması XLSX değil (%d bayt), atlandı: %s", len(r.content), dl)
        return None
    log.info("Wayback kopyası indirildi (%s): %s", ts, original)
    return r.content, ts


def _ts_to_iso(ts: str) -> str | None:
    try:
        return datetime.strptime(ts[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _is_fresh(path: Path, max_age_days: int) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age <= max_age_days * 86400


def default_data_dir() -> Path:
    """Elle indirilen listelerin dizini: ``data/argem``."""
    return get_settings().abs_data_dir / "argem"


def default_cache_dir() -> Path:
    """Otomatik indirme önbelleği: ``data/cache/argem``."""
    return get_settings().abs_data_dir / "cache" / "argem"


def fetch_xlsx(
    kind: str = KIND_ARGE,
    *,
    client: httpx.Client | None = None,
    cache_dir: Path | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    use_direct: bool = True,
    use_wayback: bool = True,
    throttle: Throttle | None = None,
    wayback_retries: int = WAYBACK_RETRIES,
    wayback_sleep_sec: float = WAYBACK_RETRY_SLEEP_SEC,
) -> Path | None:
    """XLSX'i önbelleğe indirir (``data/cache/argem/``) ve yolunu döner. Taze önbellek varsa ağa çıkmaz.

    Hata durumunda None döner, exception fırlatmaz.
    """
    url = OFFICIAL_URLS.get(kind)
    if url is None:
        log.warning("Bilinmeyen liste türü: %r (beklenen: %s)", kind, " / ".join(OFFICIAL_URLS))
        return None
    cache_dir = cache_dir or default_cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Önbellek dizini oluşturulamadı (%s): %s", cache_dir, e)
        return None
    target = cache_dir / OFFICIAL_FILE_NAMES[kind]
    if target.exists() and _is_fresh(target, max_age_days):
        log.info("Önbellekteki %s listesi kullanılıyor: %s", kind, target)
        return target

    own_client = client is None
    client = client or make_http_client()
    data: bytes | None = None
    meta: dict[str, Any] = {"url": url, "tur": kind}
    try:
        if use_direct:
            data = download_direct(url, client, throttle)
            if data:
                meta["kaynak"] = "dogrudan"
        if data is None and use_wayback:
            res = download_wayback(url, client, throttle=throttle, retries=wayback_retries, sleep_sec=wayback_sleep_sec)
            if res:
                data, ts = res
                meta.update(kaynak="wayback", yakalama=ts, dosya_tarihi=_ts_to_iso(ts))
    except Exception as e:  # ağ katmanında beklenmeyen hata: akış durmasın
        log.warning("%s listesi indirilemedi: %s", kind, e)
        data = None
    finally:
        if own_client:
            client.close()

    if data is None:
        if target.exists():
            log.warning("%s listesi güncellenemedi; eski önbellek kullanılıyor: %s", kind, target)
            return target
        return None
    try:
        target.write_bytes(data)
        meta["indirme_zamani"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Önbelleğe yazılamadı (%s): %s", target, e)
        return None
    log.info("%s listesi indirildi (%d bayt): %s", kind, len(data), target)
    return target


def cache_meta(path: Path) -> dict[str, Any]:
    """``fetch_xlsx`` tarafından yazılan yan bilgi dosyasını okur (yoksa boş sözlük)."""
    meta = path.with_suffix(".json")
    try:
        if meta.exists():
            data = json.loads(meta.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def local_xlsx_files(data_dir: Path) -> list[Path]:
    """Dizindeki (geçici olmayan) xlsx dosyalarını ada göre sıralı döner."""
    if not data_dir.is_dir():
        return []
    return sorted(p for p in data_dir.glob("*.xlsx") if not p.name.startswith("~$"))


def manual_download_instructions(data_dir: Path | None = None) -> str:
    """Kullanıcıya gösterilecek elle indirme yönergesi (Türkçe)."""
    d = data_dir or default_data_dir()
    return (
        "Ar-Ge / Tasarım Merkezi listeleri otomatik indirilemedi (sanayi.gov.tr tarayıcı dışı erişimi engelliyor).\n"
        f"Lütfen tarayıcınızla şu dosyaları indirip '{d}' klasörüne kaydedin:\n"
        f"  - {OFFICIAL_URLS[KIND_ARGE]}\n"
        f"  - {OFFICIAL_URLS[KIND_TASARIM]}\n"
        f"Adresler değişmişse istatistik sayfasındaki 'Liste' bağlantısını izleyin: {STATS_PAGE_URL}"
    )


# --- collector ---------------------------------------------------------------------------

class ArgemCollector:
    """Ar-Ge / Tasarım Merkezi listelerini okuyan collector (name="argem").

    Parametreler:
    * ``path``: tek xlsx dosyası, dizin ya da yol listesi; verilirse yalnızca bunlar okunur (ağa çıkılmaz).
    * ``only_tech``: True (varsayılan) ise sektör/ad teknoloji ipucu taşımayan satırlar atılır.
    * ``wide_sectors``: True ise makine/enerji/sağlık gibi geniş küme de kabul edilir.
    * ``include_tasarim``: Tasarım Merkezleri dosyasını da indir/oku.
    * ``allow_network``: yerel dosya yoksa indirme dene (doğrudan + Wayback).
    * ``data_dir`` / ``cache_dir``: varsayılan ``data/argem`` ve ``data/cache/argem``.
    """

    name = SOURCE_NAME

    def __init__(
        self,
        path: str | Path | Sequence[str | Path] | None = None,
        *,
        only_tech: bool = True,
        wide_sectors: bool = False,
        include_tasarim: bool = True,
        allow_network: bool = True,
        use_wayback: bool = True,
        data_dir: Path | None = None,
        cache_dir: Path | None = None,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        client: httpx.Client | None = None,
        wayback_sleep_sec: float = WAYBACK_RETRY_SLEEP_SEC,
    ):
        if path is None:
            self.paths: list[Path] | None = None
        elif isinstance(path, (str, Path)):
            self.paths = [Path(path)]
        else:
            self.paths = [Path(p) for p in path]
        self.only_tech = only_tech
        self.wide_sectors = wide_sectors
        self.include_tasarim = include_tasarim
        self.allow_network = allow_network
        self.use_wayback = use_wayback
        self._data_dir = data_dir
        self._cache_dir = cache_dir
        self.max_age_days = max_age_days
        self.client = client
        self.wayback_sleep_sec = wayback_sleep_sec
        self.last_files: list[Path] = []

    # -- dosya çözümleme ---------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self._data_dir or default_data_dir()

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir or default_cache_dir()

    def _explicit_files(self) -> list[Path]:
        files: list[Path] = []
        for p in self.paths or []:
            if p.is_dir():
                files.extend(local_xlsx_files(p))
            elif p.is_file():
                files.append(p)
            else:
                log.warning("Ar-Ge listesi dosyası bulunamadı: %s", p)
        return files

    def resolve_files(self) -> list[Path]:
        """Okunacak xlsx dosyalarını belirler: açık yol > data/argem > önbellek > ağ."""
        if self.paths is not None:
            if not self.paths:
                log.warning("Ar-Ge listesi için boş yol listesi verildi; kaynak atlanıyor")
            return self._explicit_files()
        files = local_xlsx_files(self.data_dir)
        if files:
            log.info("Elle indirilen Ar-Ge/Tasarım listeleri kullanılıyor (%d dosya): %s", len(files), self.data_dir)
            return files
        kinds = [KIND_ARGE] + ([KIND_TASARIM] if self.include_tasarim else [])
        found: list[Path] = []
        if self.allow_network:
            client = self.client or make_http_client()
            throttle = Throttle()
            try:
                for kind in kinds:
                    p = fetch_xlsx(
                        kind,
                        client=client,
                        cache_dir=self.cache_dir,
                        max_age_days=self.max_age_days,
                        use_wayback=self.use_wayback,
                        throttle=throttle,
                        wayback_sleep_sec=self.wayback_sleep_sec,
                    )
                    if p is not None:
                        found.append(p)
            finally:
                if self.client is None:
                    client.close()
        else:
            for kind in kinds:
                p = self.cache_dir / OFFICIAL_FILE_NAMES[kind]
                if p.exists():
                    found.append(p)
        if not found:
            log.warning(manual_download_instructions(self.data_dir))
        return found

    # -- toplama ---------------------------------------------------------------------------

    def collect(self, cities: list[str] | None = None) -> Iterator[RawCompany]:
        """Dosyaları okur, filtreler, tekilleştirir ve RawCompany üretir. Hata olursa loglar ve boş döner."""
        self.last_files = []
        try:
            files = self.resolve_files()
        except Exception as e:  # beklenmeyen hata: kaynak atlanır
            log.exception("Ar-Ge listesi dosyaları belirlenemedi: %s", e)
            return
        self.last_files = files
        if not files:
            return
        records: list[ArgemRecord] = []
        for f in files:
            try:
                meta = cache_meta(f)
                recs = parse_xlsx_records(f, file_date=meta.get("dosya_tarihi"))
            except Exception as e:  # bozuk dosya vb.
                log.warning("Ar-Ge listesi okunamadı (%s): %s", f, e)
                continue
            log.info("%s: %d satır okundu", f.name, len(recs))
            records.extend(recs)
        records = filter_records_by_cities(records, cities)
        if self.only_tech:
            before = len(records)
            records = [r for r in records if is_tech_record(r, wide=self.wide_sectors)]
            log.info("Sektör filtresi: %d -> %d kayıt", before, len(records))
        records = dedupe_records(records)
        log.info("Ar-Ge/Tasarım merkezi kaydı üretildi: %d", len(records))
        for r in records:
            yield record_to_raw(r)


def collect(cities: list[str] | None = None, **kw) -> Iterator[RawCompany]:
    """Kısayol: ``ArgemCollector(**kw).collect(cities)``."""
    return ArgemCollector(**kw).collect(cities)
