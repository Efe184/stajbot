"""CSV / XLSX içe aktarma: okul protokol listesi ya da elle toplanan şirket listesi -> RawCompany.

Başlıklar İngilizce (``name, website, email, city, ...``) ya da Türkçe (``Şirket Adı, Web Sitesi, E-posta,
Şehir, ...``) olabilir; yalnızca ad zorunludur. CSV ayracı (virgül / noktalı virgül / sekme) otomatik
seçilir; UTF-8 (BOM'lu da olur), UTF-16 (Excel "Unicode Metin") ve Türkçe Excel'in cp1254 kodlaması
denenir. XLSX için ilk sayfa okunur; başlık satırı ilk 10 satır içinde aranır.

CLI (``import-csv``) bu modüldeki ``read_file`` fonksiyonunu kullanır.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Iterable

from ..enums import ascii_fold
from ..log import get_logger
from ..utils import clean_text, domain_of, is_valid_email, normalize_url
from .base import RawCompany

log = get_logger(__name__)

TEMPLATE_COLUMNS: tuple[str, ...] = (
    "name", "website", "email", "city", "district", "address", "phone",
    "sector", "description", "contact_name", "tags", "notes",
)

HEADER_ALIASES: dict[str, str] = {
    "name": "name", "ad": "name", "adi": "name", "isim": "name", "sirket": "name", "sirket adi": "name",
    "sirket ismi": "name", "sirket unvani": "name", "firma": "name", "firma adi": "name", "firma ismi": "name",
    "firma unvani": "name", "unvan": "name", "unvani": "name", "ticari unvan": "name", "ticaret unvani": "name",
    "kurum": "name", "kurum adi": "name", "kurulus": "name", "kurulus adi": "name", "isletme": "name",
    "isletme adi": "name", "company": "name", "company name": "name",
    "website": "website", "web": "website", "site": "website", "web sitesi": "website", "url": "website",
    "websitesi": "website", "web adresi": "website", "web site": "website", "internet sitesi": "website",
    "internet adresi": "website", "www": "website", "homepage": "website",
    "email": "email", "e-mail": "email", "e mail": "email", "eposta": "email", "e-posta": "email", "e posta": "email",
    "mail": "email", "eposta adresi": "email", "e posta adresi": "email", "e mail adresi": "email",
    "mail adresi": "email", "email adresi": "email", "elektronik posta": "email", "iletisim e posta": "email",
    "city": "city", "sehir": "city", "il": "city", "ili": "city", "sehri": "city", "il adi": "city",
    "district": "district", "ilce": "district", "ilcesi": "district",
    "address": "address", "adres": "address", "adresi": "address", "acik adres": "address",
    "phone": "phone", "telefon": "phone", "tel": "phone", "telefon no": "phone", "tel no": "phone",
    "telefon numarasi": "phone", "gsm": "phone", "cep": "phone", "cep telefonu": "phone",
    "sector": "sector", "sektor": "sector", "sektoru": "sector", "faaliyet alani": "sector",
    "description": "description", "aciklama": "description", "tanim": "description",
    "contact_name": "contact_name", "contact name": "contact_name", "contact": "contact_name", "kisi": "contact_name",
    "yetkili": "contact_name", "yetkili kisi": "contact_name", "ilgili": "contact_name", "ilgili kisi": "contact_name",
    "iletisim kisisi": "contact_name", "irtibat kisisi": "contact_name", "ad soyad": "contact_name",
    "tags": "tags", "etiketler": "tags", "etiket": "tags",
    "notes": "notes", "not": "notes", "notlar": "notes", "note": "notes",
}

# Aynı dosyada birden çok sütunu birleştirilebilen alanlar ("E-posta", "E-posta 2").
_MULTI_FIELDS: frozenset[str] = frozenset({"email", "tags", "phone"})

SCHOOL_TAG = "okul-protokol"

_HEADER_JUNK_RE = re.compile(r"[^a-z0-9 ]+")
_TRAILING_NUMBER_RE = re.compile(r"\s+\d+$")
_URL_SPLIT_RE = re.compile(r"[,\s;|]+")
_XLS_SUFFIXES = (".xls", ".xlt", ".ods", ".numbers")
_XLSX_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")


def _fold_header(h: Any) -> str:
    """Başlığı eşleştirme anahtarına indirger: ASCII, küçük harf, noktalama yok, sondaki sayı yok."""
    s = ascii_fold(str(h or "")).replace("_", " ").replace("-", " ")
    s = _HEADER_JUNK_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _TRAILING_NUMBER_RE.sub("", s).strip()
    return s


def map_headers(headers: Iterable[Any]) -> dict[int, str]:
    """Sütun indeksi -> kanonik alan adı. E-posta/etiket/telefon için birden çok sütun olabilir."""
    out: dict[int, str] = {}
    for i, h in enumerate(headers):
        key = HEADER_ALIASES.get(_fold_header(h))
        if key and (key in _MULTI_FIELDS or key not in out.values()):
            out[i] = key
    return out


def split_list(val: str | None) -> list[str]:
    if not val:
        return []
    parts = [p.strip() for p in str(val).replace(";", ",").replace("|", ",").replace("/", ",").split(",")]
    out: list[str] = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


def split_emails(val: str | None) -> list[str]:
    out: list[str] = []
    for e in str(val or "").replace(";", " ").replace(",", " ").replace("|", " ").split():
        e = e.strip().lower().strip("<>()[]\"'.,:;")
        if e.startswith("mailto:"):
            e = e[7:]
        if is_valid_email(e) and e not in out:
            out.append(e)
    return out


def first_website(val: str | None) -> str | None:
    """Hücredeki ilk geçerli web adresini normalize eder (``"www.x.com, www.y.com"`` -> ``https://www.x.com``)."""
    text = clean_text(val)
    if not text:
        return None
    for cand in [text] + [p for p in _URL_SPLIT_RE.split(text) if p]:
        url = normalize_url(cand)
        if url and domain_of(url) is not None:
            return url
    return None


def row_to_raw(rec: dict[str, Any], *, source: str, default_city: str | None, source_url: str | None, row_no: int) -> RawCompany | None:
    name = clean_text(rec.get("name"), 255)
    if not name:
        return None
    tags = split_list(rec.get("tags"))
    if source == "school" and SCHOOL_TAG not in tags:
        tags.append(SCHOOL_TAG)
    raw: dict[str, Any] = {"satir": row_no}
    for k in ("notes", "contact_name"):
        v = clean_text(rec.get(k))
        if v:
            raw[k] = v
    return RawCompany(
        name=name,
        source=source,
        source_url=source_url,
        website=first_website(rec.get("website")),
        city=clean_text(rec.get("city")) or default_city,
        district=clean_text(rec.get("district"), 64),
        address=clean_text(rec.get("address")),
        phone=clean_text(rec.get("phone"), 64),
        sector=clean_text(rec.get("sector"), 128),
        description=clean_text(rec.get("description")),
        tags=tags,
        emails=split_emails(rec.get("email")),
        school_protocol=(source == "school"),
        raw=raw,
    )


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1254"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _delimiter(text: str) -> str:
    """Ayracı seçer: ilk 10 dolu satırı her adayla ayrıştırıp en çok tanınan başlık sütununu veren kazanır.

    Eşitlikte en çok geçen karakter seçilir. Böylece başlık üstünde başlık dışı satır ("Okul Protokol
    Listesi") ya da hücre içinde virgüllü adresler ayracı şaşırtmaz.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:10]
    sample = "\n".join(lines)
    best, best_score = ",", (-1, -1)
    for d in (";", ",", "\t"):
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=d, skipinitialspace=True))
        except csv.Error:
            rows = []
        recognised = max((len(map_headers(r)) for r in rows[:10]), default=0)
        score = (recognised, sample.count(d))
        if score > best_score:
            best, best_score = d, score
    return best


def read_csv(path: Path, *, source: str = "csv", default_city: str | None = None) -> list[RawCompany]:
    text = _read_text(path)
    if not text.strip():
        log.warning("Boş dosya: %s", path)
        return []
    reader = csv.reader(io.StringIO(text), delimiter=_delimiter(text), skipinitialspace=True)
    rows = list(reader)
    return rows_to_raws(rows, source=source, default_city=default_city, source_url=_file_uri(path), label=path.name)


def _cell_to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "evet" if v else "hayır"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))  # 5321234567.0 -> "5321234567" (telefon / sayı hücreleri)
    return str(v)


def read_xlsx(path: Path, *, source: str = "csv", default_city: str | None = None) -> list[RawCompany]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0] if wb.worksheets else None
        rows: list[list[Any]] = []
        if ws is not None:
            for r in ws.iter_rows(values_only=True):
                rows.append([_cell_to_str(v) for v in r])
    finally:
        wb.close()
    return rows_to_raws(rows, source=source, default_city=default_city, source_url=_file_uri(path), label=path.name)


def _collect_record(row: list[Any], colmap: dict[int, str]) -> dict[str, Any]:
    """Sütun haritasına göre satırı kayda çevirir; çoklu sütunlar (e-posta 1/2) birleştirilir."""
    rec: dict[str, Any] = {}
    for i, key in colmap.items():
        val = row[i] if i < len(row) else None
        if val is None or not str(val).strip():
            continue
        if key in rec and rec[key] is not None and str(rec[key]).strip():
            if key in _MULTI_FIELDS:
                rec[key] = f"{rec[key]}, {val}"
            continue
        rec[key] = val
    return rec


def rows_to_raws(rows: list[list[Any]], *, source: str, default_city: str | None, source_url: str | None, label: str = "") -> list[RawCompany]:
    """Başlık satırını bulur (ilk 10 satır içinde) ve kalan satırları RawCompany'ye çevirir."""
    header_idx = None
    colmap: dict[int, str] = {}
    for i, row in enumerate(rows[:10]):
        m = map_headers(row)
        if "name" in m.values():
            header_idx, colmap = i, m
            break
    if header_idx is None:
        raise ValueError(
            f"{label}: şirket adı sütunu bulunamadı. Beklenen başlıklar: {', '.join(TEMPLATE_COLUMNS)}"
        )
    out: list[RawCompany] = []
    for n, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        rec = _collect_record(row, colmap)
        raw = row_to_raw(rec, source=source, default_city=default_city, source_url=source_url, row_no=n)
        if raw is None:
            if any(str(v or "").strip() for v in row):
                log.debug("%s satır %d: ad boş, atlandı", label, n)
            continue
        out.append(raw)
    log.info("%s: %d satır okundu (kaynak: %s)", label, len(out), source)
    return out


def _file_uri(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except (ValueError, OSError):
        return f"file:///{path.as_posix().lstrip('/')}"


def read_file(path: Path | str, *, source: str = "csv", default_city: str | None = None) -> list[RawCompany]:
    """CSV ya da XLSX dosyasını okur. ``source`` 'school' ise okul protokolü bayrağı ve etiketi eklenir."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Dosya bulunamadı: {p}")
    suffix = p.suffix.lower()
    if suffix in _XLSX_SUFFIXES:
        return read_xlsx(p, source=source, default_city=default_city)
    if suffix in _XLS_SUFFIXES:
        raise ValueError(f"{p.name}: bu biçim desteklenmiyor; dosyayı Excel'den .xlsx ya da CSV UTF-8 olarak kaydedin.")
    return read_csv(p, source=source, default_city=default_city)


def template_text() -> str:
    header = ",".join(TEMPLATE_COLUMNS)
    example = (
        "Örnek Yazılım A.Ş.,https://www.ornekyazilim.com,kariyer@ornekyazilim.com,İstanbul,Maslak,"
        ",0212 000 00 00,Fintech,Ödeme sistemleri geliştirir,,\"fintech;odeme\",Okul listesinden"
    )
    return header + "\n" + example + "\n"
