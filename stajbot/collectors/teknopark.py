"""Teknopark / teknokent firma rehberlerini toplayan, YAML ile yapılandırılan collector.

Yapılandırma: ``stajbot/collectors/teknoparklar.yaml`` (şema dosyanın başında açıklanır).
Üç kaynak türü desteklenir: sunucu tarafında üretilen HTML listeleri (``kind: html``),
açık JSON uç noktaları (``kind: json``) ve HTML içine gömülü JSON (``kind: inline_json``).
``kind: js_unsupported`` olan kayıtlar (tarayıcı gerektiren siteler) uyarı ile atlanır.

Çevrimdışı testler için ``parse_list_html``, ``parse_json``, ``extract_inline_json``,
``extract_sub_links`` ve ``apply_detail_html`` / ``apply_detail_json`` dışa açıktır.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx
import yaml
from bs4 import BeautifulSoup
from bs4.element import Tag

from ..config import get_settings
from ..enums import ascii_fold, normalize_city
from ..log import get_logger
from ..utils import clean_text, domain_of, is_valid_email, normalize_name, normalize_url
from .base import RawCompany, Throttle, make_http_client, safe_get

log = get_logger(__name__)

SOURCE_NAME = "teknopark"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("teknoparklar.yaml")

VALID_KINDS = {"html", "json", "inline_json", "js_unsupported"}
REQUIRED_KEYS = ("slug", "name", "city", "kind", "list_url")
# RawCompany üzerinde doğrudan doldurulabilen alanlar
_FIELD_NAMES = ("name", "website", "sector", "phone", "email", "description", "address", "district")
# Ham veride tutulan yardımcı alanlar
_RAW_FIELDS = ("detail_link", "detail_id", "legal_name", "building")

_DEFAULTS = {"max_detail_pages": 150, "max_pages": 60}


# ---------------------------------------------------------------------------
# Yapılandırma
# ---------------------------------------------------------------------------


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """YAML yapılandırmasını okur ve ``{"defaults": {...}, "technoparks": [...]}`` döner."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        log.warning("Teknopark yapılandırması (%s) bir sözlük değil, kayıt okunamadı.", path)
        data = {}
    defaults = dict(_DEFAULTS)
    raw_defaults = data.get("defaults")
    if isinstance(raw_defaults, dict):
        defaults.update(raw_defaults)
    entries: list[dict[str, Any]] = []
    raw_entries = data.get("technoparks") or []
    if not isinstance(raw_entries, list):
        log.warning("Teknopark yapılandırmasında 'technoparks' bir liste değil, kayıt okunamadı.")
        raw_entries = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        missing = [k for k in REQUIRED_KEYS if not entry.get(k)]
        if missing:
            log.warning("Teknopark yapılandırması eksik alan içeriyor (%s): %s", ", ".join(missing), entry.get("slug") or entry)
            continue
        e = dict(entry)
        e.setdefault("pagination", "none")
        e.setdefault("max_detail_pages", defaults["max_detail_pages"])
        e.setdefault("max_pages", defaults["max_pages"])
        entries.append(e)
    return {"defaults": defaults, "technoparks": entries}


def list_technoparks(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Yapılandırmadaki teknopark kayıtlarını döner."""
    return load_config(config_path)["technoparks"]


def _city_key(city: str | None) -> str:
    return ascii_fold(normalize_city(city) or "")


def select_entries(
    entries: list[dict[str, Any]],
    cities: list[str] | None = None,
    slugs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Şehir ve slug süzgeçlerini uygular (şehir eşleşmesi Türkçe karaktere duyarsızdır)."""
    out = list(entries)
    # Boş/boşluk slug ve şehir adları süzgeç sayılmaz (yoksa her şey elenirdi).
    wanted = {str(s).strip().lower() for s in (slugs or []) if s and str(s).strip()}
    if wanted:
        out = [e for e in out if str(e.get("slug", "")).lower() in wanted]
    wanted_cities = {k for k in (_city_key(c) for c in (cities or []) if c) if k}
    if wanted_cities:
        out = [e for e in out if _city_key(e.get("city")) in wanted_cities]
    return out


# ---------------------------------------------------------------------------
# Seçici (selector) yardımcıları
# ---------------------------------------------------------------------------


def _parse_spec(spec: Any) -> list[dict[str, Any]]:
    """Alan tanımını normalize eder: her zaman {sel, attr, regex, index} sözlüklerinin listesi."""
    if spec is None:
        return []
    if isinstance(spec, list):
        out: list[dict[str, Any]] = []
        for s in spec:
            out.extend(_parse_spec(s))
        return out
    if isinstance(spec, dict):
        return [{"sel": spec.get("sel", "."), "attr": spec.get("attr"), "regex": spec.get("regex"), "index": spec.get("index")}]
    s = str(spec).strip()
    if not s:
        return []
    attr = None
    if s.startswith("@"):
        return [{"sel": ".", "attr": s[1:], "regex": None, "index": None}]
    if "@" in s:
        # Son '@' özniteliği ayırır; seçici içindeki [attr^='x'] kalıplarında '@' bulunmaz.
        sel, attr = s.rsplit("@", 1)
        return [{"sel": sel.strip() or ".", "attr": attr.strip() or None, "regex": None, "index": None}]
    return [{"sel": s, "attr": None, "regex": None, "index": None}]


def _select(node: Tag, sel: str, index: int | None) -> Tag | None:
    if sel in ("", "."):
        return node
    try:
        if index is None:
            return node.select_one(sel)
        found = node.select(sel)
        return found[index] if found and -len(found) <= index < len(found) else None
    except Exception as exc:  # geçersiz CSS vb.
        log.debug("Seçici hatası (%s): %s", sel, exc)
        return None


def _extract(node: Tag, spec: Any) -> str | None:
    """Bir öğeden alan değerini çıkarır; hiçbir aday dolu değer vermezse None."""
    for part in _parse_spec(spec):
        el = _select(node, part["sel"], part["index"])
        if el is None:
            continue
        if part["attr"]:
            val = el.get(part["attr"])
            if isinstance(val, list):
                val = " ".join(val)
        else:
            # Satırları (<br>, <p>) koru: regex'ler satır bazında çalışabilsin.
            val = el.get_text("\n", strip=True)
        if val is None:
            continue
        val = str(val).strip()
        if part["regex"]:
            try:
                m = re.search(part["regex"], val, flags=re.IGNORECASE | re.MULTILINE)
            except re.error as exc:
                log.debug("Geçersiz düzenli ifade (%s): %s", part["regex"], exc)
                continue
            # İsteğe bağlı grup eşleşmezse group(1) None olur; bu bir öğeyi düşürmemeli.
            val = ((m.group(1) if m.groups() else m.group(0)) or "").strip() if m else ""
        val = re.sub(r"\s+", " ", val).strip()
        if val:
            return val
    return None


def _soup(html: str | bytes) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _own_domains(entry: dict[str, Any] | None, base_url: str | None = None) -> set[str]:
    """Teknoparkın kendi alan adları: firma web sitesi olarak kabul edilmez."""
    out: set[str] = set()
    for u in ((entry or {}).get("list_url"), base_url):
        d = domain_of(u)
        if d:
            out.add(d)
    return out


def _clean_website(url: str | None, base_url: str | None = None, reject_domains: set[str] | None = None) -> str | None:
    """Web adresini normalize eder; alan adı çıkmıyorsa (ör. 'http://www.') ya da adres
    teknoparkın kendi sitesine (göreli bağlantı, firma detay sayfası) gidiyorsa None döner."""
    if not url:
        return None
    u = str(url).strip()
    if not u or u.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    if base_url and u.startswith("/"):
        u = urljoin(base_url, u)
    u = normalize_url(u)
    if not u:
        return None
    dom = domain_of(u)
    if not dom:
        return None
    if reject_domains and dom in reject_domains:
        return None
    return u


def _clean_email(val: str | None) -> str | None:
    if not val:
        return None
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", val)
    if not m:
        return None
    email = m.group(0).lower()
    return email if is_valid_email(email) else None


# Yalnızca hukuki biçim ekleri; "yazılım", "teknoloji" gibi tanımlayıcı sözcükler korunur ki
# aynı teknoparktaki "Alfa Yazılım" ile "Alfa Teknoloji" farklı firmalar olarak kalsın.
_LEGAL_FORM_TOKENS = frozenset(
    {
        "a.s", "as", "anonim", "sirketi", "sirket", "ltd", "sti", "limited", "san", "tic", "ve",
        "sanayi", "ticaret", "inc", "llc", "gmbh", "co", "corp", "s.a", "ag",
    }
)


def _dedupe_key(name: str) -> str:
    """Teknopark içi tekilleştirme anahtarı: ASCII, küçük harf, noktalama ve hukuki ekler yok."""
    s = ascii_fold(name or "")
    s = s.replace("&", " ve ")
    s = re.sub(r"[^a-z0-9\s.]", " ", s)
    tokens = [t for t in re.split(r"\s+", s) if t]
    cleaned = [t for t in tokens if t.strip(".") not in _LEGAL_FORM_TOKENS]
    if not cleaned:
        cleaned = tokens
    key = " ".join(t.strip(".") for t in cleaned).strip()
    return key or normalize_name(name) or ascii_fold(name or "")


def _abs_url(href: str | None, base_url: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if base_url:
        return urljoin(base_url, href)
    return href


# ---------------------------------------------------------------------------
# HTML liste ayrıştırma
# ---------------------------------------------------------------------------


def _new_company(entry: dict[str, Any], name: str) -> RawCompany:
    return RawCompany(
        name=name,
        source=SOURCE_NAME,
        source_url=entry.get("list_url"),
        city=entry.get("city"),
        is_teknopark=True,
        teknopark_name=entry.get("name"),
        raw={"teknopark_slug": entry.get("slug")},
    )


def _apply_context(company: RawCompany, context: dict[str, Any] | None) -> None:
    """Liste/indeks bağlamından gelen alanları (sector, building) boşsa doldurur."""
    if not context:
        return
    for key, value in context.items():
        if not value:
            continue
        if key == "building":
            company.raw["building"] = value
            if value not in company.tags:
                company.tags.append(value)
        elif key in _FIELD_NAMES and key != "name":
            if not getattr(company, key):
                setattr(company, key, value)
        else:
            company.raw.setdefault(key, value)


def parse_list_html(
    html: str | bytes,
    entry: dict[str, Any],
    base_url: str | None = None,
    context: dict[str, Any] | None = None,
) -> list[RawCompany]:
    """Bir liste sayfasının HTML'inden RawCompany listesi üretir (çevrimdışı test edilebilir).

    Seçicisi eksik/uyumsuz alanlar sessizce boş kalır; adı bulunamayan öğeler loglanıp atlanır.
    """
    selectors = entry.get("selectors") or {}
    item_sel = selectors.get("item")
    base_url = base_url or entry.get("list_url")
    if not item_sel:
        log.warning("%s: 'selectors.item' tanımlı değil, liste ayrıştırılamıyor.", entry.get("name"))
        return []
    try:
        soup = _soup(html)
        items = soup.select(item_sel)
    except Exception as exc:
        log.warning("%s: HTML ayrıştırılamadı: %s", entry.get("name"), exc)
        return []

    page_context: dict[str, Any] = dict(context or {})
    for key, spec in (entry.get("context_selectors") or {}).items():
        if not page_context.get(key):
            val = _extract(soup, spec)
            if val:
                page_context[key] = clean_text(val, 128)

    companies: list[RawCompany] = []
    skipped = 0
    own = _own_domains(entry, base_url)
    for item in items:
        try:
            name = clean_text(_extract(item, selectors.get("name")), 255)
            if not name:
                skipped += 1
                continue
            c = _new_company(entry, name)
            for field in _FIELD_NAMES:
                if field == "name" or field not in selectors:
                    continue
                val = _extract(item, selectors[field])
                if val is None:
                    continue
                if field == "website":
                    c.website = _clean_website(val, base_url, own)
                elif field == "email":
                    em = _clean_email(val)
                    if em and em not in c.emails:
                        c.emails.append(em)
                else:
                    setattr(c, field, clean_text(val, 2000 if field == "description" else 255))
            for field in _RAW_FIELDS:
                if field in selectors:
                    val = _extract(item, selectors[field])
                    if val:
                        if field == "detail_link":
                            val = _abs_url(val, base_url)
                        c.raw[field] = clean_text(val, 512)
            _apply_context(c, page_context)
            companies.append(c)
        except Exception as exc:  # tek öğe hatası tüm sayfayı düşürmesin
            skipped += 1
            log.debug("%s: öğe ayrıştırılamadı: %s", entry.get("name"), exc)
    if skipped:
        log.info("%s: adı okunamayan %d öğe atlandı.", entry.get("name"), skipped)
    return companies


def extract_sub_links(html: str | bytes, entry: dict[str, Any], base_url: str | None = None) -> list[tuple[str, str | None]]:
    """İndeks sayfasından alt liste bağlantılarını ``(mutlak_url, etiket)`` olarak döner (tekil, sıralı)."""
    sel = entry.get("sub_links")
    if not sel:
        return []
    base_url = base_url or entry.get("list_url")
    pattern = entry.get("sub_links_pattern")
    rx = None
    if pattern:
        try:
            rx = re.compile(str(pattern))
        except re.error as exc:
            log.warning("%s: 'sub_links_pattern' geçersiz bir düzenli ifade (%s), alt bağlantılar okunamadı.", entry.get("name"), exc)
            return []
    labels: dict[str, str] = {}
    order: list[str] = []
    try:
        soup = _soup(html)
        anchors = soup.select(sel)
    except Exception as exc:
        log.warning("%s: alt bağlantılar okunamadı: %s", entry.get("name"), exc)
        return []
    for a in anchors:
        href = a.get("href") if isinstance(a, Tag) else None
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        href = str(href).strip()
        if rx and not rx.search(href):
            continue
        url = _abs_url(href, base_url) or href
        if url.rstrip("/") == str(base_url).rstrip("/"):
            continue
        text = clean_text(a.get_text(" ", strip=True) or a.get("title"), 128) or ""
        if url not in labels:
            labels[url] = text
            order.append(url)
        elif len(text) > len(labels[url]):
            labels[url] = text  # en uzun (dolu) metni etiket olarak kullan
    return [(u, labels[u] or None) for u in order]


# ---------------------------------------------------------------------------
# JSON ayrıştırma
# ---------------------------------------------------------------------------


def _json_path(data: Any, path: Any) -> Any:
    """Nokta ayrılmış yol ile JSON içinde gezinir; yol liste ise ilk dolu değeri döner."""
    if path is None:
        return None
    if isinstance(path, list):
        for p in path:
            v = _json_path(data, p)
            if v not in (None, ""):
                return v
        return None
    cur = data
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def _json_scalar(val: Any, null_values: set[str]) -> str | None:
    if val is None or isinstance(val, (dict, list)):
        return None
    s = str(val).strip()
    if s in null_values:
        return None
    return s or None


def parse_json(data: Any, entry: dict[str, Any]) -> list[RawCompany]:
    """JSON verisinden (``json_keys`` eşlemesi ile) RawCompany listesi üretir."""
    keys = entry.get("json_keys") or {}
    null_values = {str(v) for v in (keys.get("null_values") or [])} | {""}
    items = _json_path(data, keys.get("items", ""))
    if isinstance(items, dict):  # {id: {...}} biçimi
        items = list(items.values())
    if not isinstance(items, list):
        log.warning("%s: JSON içinde kayıt listesi bulunamadı (items=%r).", entry.get("name"), keys.get("items"))
        return []

    # kod -> ad çevirileri (ör. Konya kategori id -> sektör adı)
    lookups: dict[str, dict[str, str]] = {}
    for field, cfg in (entry.get("lookups") or {}).items():
        table = _json_path(data, cfg.get("list"))
        mapping: dict[str, str] = {}
        if isinstance(table, list):
            for row in table:
                if isinstance(row, dict):
                    k = _json_scalar(row.get(cfg.get("key", "id")), set())
                    v = _json_scalar(row.get(cfg.get("value", "name")), set())
                    if k is not None and v is not None:
                        mapping[k] = v
        lookups[field] = mapping

    companies: list[RawCompany] = []
    skipped = 0
    own = _own_domains(entry)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            name = clean_text(_json_scalar(_json_path(item, keys.get("name", "name")), null_values), 255)
            if not name:
                skipped += 1
                continue
            c = _new_company(entry, name)
            c.raw["index"] = idx
            for field in _FIELD_NAMES:
                if field == "name" or field not in keys:
                    continue
                val = _json_scalar(_json_path(item, keys[field]), null_values)
                if val is None:
                    continue
                if field in lookups:
                    val = lookups[field].get(val, val if not val.isdigit() else None)
                    if val is None:
                        continue
                if field == "website":
                    c.website = _clean_website(val, None, own)
                elif field == "email":
                    em = _clean_email(val)
                    if em and em not in c.emails:
                        c.emails.append(em)
                elif field == "description":
                    c.description = clean_text(_strip_html(val), 2000)
                else:
                    setattr(c, field, clean_text(val, 255))
            if "employee_count" in keys:
                ec = _json_scalar(_json_path(item, keys["employee_count"]), null_values)
                if ec and ec.isdigit() and int(ec) > 0:
                    c.employee_count = int(ec)
            for field in _RAW_FIELDS:
                if field in keys:
                    val = _json_scalar(_json_path(item, keys[field]), null_values)
                    if val:
                        c.raw[field] = val
            companies.append(c)
        except Exception as exc:
            skipped += 1
            log.debug("%s: JSON kaydı ayrıştırılamadı: %s", entry.get("name"), exc)
    if skipped:
        log.info("%s: adı okunamayan %d JSON kaydı atlandı.", entry.get("name"), skipped)
    return companies


def _strip_html(text: str) -> str:
    if "<" in text and ">" in text:
        try:
            return BeautifulSoup(text, "lxml").get_text(" ", strip=True)
        except Exception:
            return text
    return text


def _loads_lenient(text: str) -> Any:
    """Ham kontrol karakteri içeren JSON'ı da kabul eder (Konya companies.asp)."""
    text = text.lstrip("﻿")
    return json.loads(text, strict=False)


def extract_inline_json(html: str, entry: dict[str, Any]) -> Any:
    """HTML içindeki ``JSON.parse('...')`` biçiminde gömülü JS dizesini çözer (Sakarya)."""
    marker = entry.get("inline_json_marker") or "JSON.parse('"
    start = html.find(marker)
    if start == -1:
        raise ValueError(f"'{marker}' işareti sayfada bulunamadı")
    i = start + len(marker)
    j = i
    while True:
        j = html.find("'", j)
        if j == -1:
            raise ValueError("gömülü JSON dizesinin sonu bulunamadı")
        backslashes = 0
        k = j - 1
        while k >= 0 and html[k] == "\\":
            backslashes += 1
            k -= 1
        if backslashes % 2 == 0:
            break
        j += 1
    return _loads_lenient(_js_unescape(html[i:j]))


_JS_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "'": "'", '"': '"', "\\": "\\", "/": "/"}


def _js_unescape(raw: str) -> str:
    """Tek tırnaklı bir JS dizesinin içeriğini çözer (\\uXXXX, \\xHH, \\n, \\' ...).

    Dize içinde kaçışsız çift tırnak bulunabilir (``JSON.parse('{"a": 1}')``); bu yüzden
    ``json.loads`` yerine elle çözülür.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt == "u" and i + 5 < n:
            hexpart = raw[i + 2 : i + 6]
            try:
                code = int(hexpart, 16)
            except ValueError:
                out.append(nxt)
                i += 2
                continue
            # UTF-16 çift (surrogate pair)
            if 0xD800 <= code <= 0xDBFF and raw[i + 6 : i + 8] == "\\u":
                try:
                    low = int(raw[i + 8 : i + 12], 16)
                except ValueError:
                    low = -1
                if 0xDC00 <= low <= 0xDFFF:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    i += 12
                    continue
            out.append(chr(code))
            i += 6
        elif nxt == "x" and i + 3 < n:
            try:
                out.append(chr(int(raw[i + 2 : i + 4], 16)))
                i += 4
            except ValueError:
                out.append(nxt)
                i += 2
        elif nxt in _JS_SIMPLE_ESCAPES:
            out.append(_JS_SIMPLE_ESCAPES[nxt])
            i += 2
        elif nxt == "\n":  # satır devamı
            i += 2
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


# ---------------------------------------------------------------------------
# Detay sayfaları
# ---------------------------------------------------------------------------


def apply_detail_html(company: RawCompany, html: str | bytes, entry: dict[str, Any], base_url: str | None = None) -> None:
    """Detay sayfası HTML'inden boş alanları doldurur (dolu alan üzerine yazılmaz)."""
    selectors = entry.get("detail_selectors") or {}
    if not selectors:
        return
    try:
        soup = _soup(html)
    except Exception as exc:
        log.debug("%s: detay HTML ayrıştırılamadı: %s", entry.get("name"), exc)
        return
    own = _own_domains(entry, base_url)
    for field, spec in selectors.items():
        try:
            val = _extract(soup, spec)
        except Exception:
            val = None
        _fill_field(company, field, val, base_url, own)


def apply_detail_json(company: RawCompany, data: Any, entry: dict[str, Any]) -> None:
    """Detay JSON yanıtından boş alanları doldurur."""
    keys = entry.get("detail_json_keys") or {}
    own = _own_domains(entry)
    for field, path in keys.items():
        val = _json_path(data, path)
        if isinstance(val, (dict, list)):
            continue
        _fill_field(company, field, None if val is None else str(val), None, own)


def _needs_detail(company: RawCompany, fields: list[str]) -> bool:
    """Detay sayfasının doldurabileceği alanlardan en az biri boşsa True."""
    if not fields:
        return False
    for f in fields:
        if f == "email":
            if not company.emails:
                return True
        elif f in _FIELD_NAMES:
            if not getattr(company, f):
                return True
        elif f not in company.raw:
            return True
    return False


def _fill_field(
    company: RawCompany, field: str, val: str | None, base_url: str | None, reject_domains: set[str] | None = None
) -> None:
    if not val:
        return
    if field == "website":
        if not company.website:
            company.website = _clean_website(val, base_url, reject_domains)
    elif field == "email":
        em = _clean_email(val)
        if em and em not in company.emails:
            company.emails.append(em)
    elif field == "description":
        if not company.description:
            company.description = clean_text(_strip_html(val), 2000)
    elif field in _FIELD_NAMES:
        if not getattr(company, field):
            setattr(company, field, clean_text(val, 255))
    else:
        company.raw.setdefault(field, clean_text(val, 512))


# ---------------------------------------------------------------------------
# Sayfalama yardımcıları
# ---------------------------------------------------------------------------


def _with_query_param(url: str, name: str, value: Any) -> str:
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    q.append((name, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _parse_pagination(spec: str | None) -> tuple[str, str | None]:
    spec = (spec or "none").strip()
    if ":" in spec:
        mode, arg = spec.split(":", 1)
        return mode.strip(), arg.strip()
    return spec, None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class TeknoparkCollector:
    """Teknopark firma rehberlerinden şirket toplar (ağa çıkar, veritabanına yazmaz)."""

    name = SOURCE_NAME

    def __init__(
        self,
        config_path: str | Path | None = None,
        slugs: list[str] | None = None,
        client: httpx.Client | None = None,
        delay: float | None = None,
        respect_robots: bool = True,
    ):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.slugs = list(slugs) if slugs else None
        self._client = client
        self._owns_client = client is None
        self._delay = delay
        self.respect_robots = respect_robots
        self._robots_cache: dict[str, robotparser.RobotFileParser | None] = {}
        self.stats: dict[str, int] = {}

    # -- genel ----------------------------------------------------------------

    @property
    def entries(self) -> list[dict[str, Any]]:
        try:
            return load_config(self.config_path)["technoparks"]
        except Exception as exc:
            log.error("Teknopark yapılandırması okunamadı (%s): %s", self.config_path, exc)
            return []

    def collect(self, cities: list[str] | None = None) -> Iterator[RawCompany]:
        """İstenen şehirlerdeki teknoparkları sırayla tarar; hata alan kaynak loglanır ve atlanır."""
        entries = select_entries(self.entries, cities=cities, slugs=self.slugs)
        if not entries:
            log.warning("Teknopark: seçilen şehir/slug için yapılandırma kaydı bulunamadı.")
            return
        client = self._client or make_http_client()
        throttle = Throttle(self._delay)
        try:
            for entry in entries:
                kind = str(entry.get("kind", "")).lower()
                if kind == "js_unsupported":
                    log.warning(
                        "%s (%s) JavaScript/bot koruması nedeniyle otomatik taranamıyor; firma listesini CSV ile içe aktarın "
                        "(python -m stajbot import-csv). Adres: %s",
                        entry["name"], entry["city"], entry.get("list_url"),
                    )
                    continue
                if kind not in VALID_KINDS:
                    log.warning("%s: bilinmeyen kaynak türü '%s', atlandı.", entry["name"], kind)
                    continue
                try:
                    companies = self._collect_entry(entry, client, throttle)
                except Exception as exc:  # kaynak bazında hata akışı durdurmaz
                    log.error("%s taranırken hata oluştu, kaynak atlandı: %s", entry["name"], exc)
                    continue
                self.stats[entry["slug"]] = len(companies)
                log.info("%s: %d firma toplandı.", entry["name"], len(companies))
                yield from companies
        finally:
            if self._owns_client:
                client.close()

    # -- kaynak bazında --------------------------------------------------------

    def _collect_entry(self, entry: dict[str, Any], client: httpx.Client, throttle: Throttle) -> list[RawCompany]:
        if not self._robots_allowed(client, entry["list_url"]):
            log.warning("%s: robots.txt bu sayfanın taranmasına izin vermiyor, kaynak atlandı.", entry["name"])
            return []
        kind = str(entry.get("kind")).lower()
        if kind == "json":
            companies = self._collect_json(entry, client, throttle)
        elif kind == "inline_json":
            companies = self._collect_inline_json(entry, client, throttle)
        else:
            companies = self._collect_html(entry, client, throttle)
        companies = self._dedupe(companies, entry)
        if entry.get("detail_page") and companies:
            self._fetch_details(companies, entry, client, throttle)
        return companies

    def _collect_json(self, entry: dict[str, Any], client: httpx.Client, throttle: Throttle) -> list[RawCompany]:
        resp = safe_get(client, entry["list_url"], throttle)
        if resp is None:
            return []
        try:
            data = _loads_lenient(resp.text)
        except ValueError as exc:
            log.warning("%s: JSON çözümlenemedi: %s", entry["name"], exc)
            return []
        return parse_json(data, entry)

    def _collect_inline_json(self, entry: dict[str, Any], client: httpx.Client, throttle: Throttle) -> list[RawCompany]:
        resp = safe_get(client, entry["list_url"], throttle)
        if resp is None:
            return []
        try:
            data = extract_inline_json(resp.text, entry)
        except ValueError as exc:
            log.warning("%s: sayfaya gömülü JSON okunamadı (site yapısı değişmiş olabilir): %s", entry["name"], exc)
            return []
        return parse_json(data, entry)

    def _collect_html(self, entry: dict[str, Any], client: httpx.Client, throttle: Throttle) -> list[RawCompany]:
        start_pages: list[tuple[str, dict[str, Any]]] = []
        if entry.get("sub_links"):
            resp = safe_get(client, entry["list_url"], throttle)
            if resp is None:
                return []
            links = extract_sub_links(resp.content, entry, str(resp.url))
            if not links:
                log.warning("%s: indeks sayfasında alt liste bağlantısı bulunamadı.", entry["name"])
                return []
            label_field = entry.get("sub_link_field") or "sector"
            for url, label in links:
                start_pages.append((url, {label_field: label} if label else {}))
            pagination = entry.get("sub_pagination") or "none"
        else:
            start_pages.append((entry["list_url"], {}))
            pagination = entry.get("pagination") or "none"

        companies: list[RawCompany] = []
        for url, context in start_pages:
            # Yinelenen sayfa koruması alt liste (sektör/bina) bazında tutulur: bir sektör
            # sayfasındaki firmaların hepsi başka bir sektörde görülmüş olsa bile o sektörün
            # sonraki sayfaları çekilmeye devam eder.
            seq_seen: set[str] = set()
            for page_companies in self._iter_pages(entry, url, pagination, context, client, throttle):
                keys = {_dedupe_key(c.name) for c in page_companies}
                companies.extend(page_companies)
                if keys and keys <= seq_seen:
                    # Sayfalama parametresi yok sayılıyor: aynı sayfa tekrar geldi.
                    break
                seq_seen |= keys
        return companies

    def _iter_pages(
        self,
        entry: dict[str, Any],
        first_url: str,
        pagination: str,
        context: dict[str, Any],
        client: httpx.Client,
        throttle: Throttle,
    ) -> Iterator[list[RawCompany]]:
        mode, arg = _parse_pagination(pagination)
        max_pages = int(entry.get("max_pages") or _DEFAULTS["max_pages"])
        page_size = entry.get("page_size")
        page_start = entry.get("page_start")
        page_no = 2 if page_start is None else int(page_start)  # 0 geçerli bir başlangıçtır
        step = int(entry.get("page_step") or 1)
        visited: set[str] = set()
        url: str | None = first_url
        for _ in range(max_pages):
            if not url or url in visited:
                break
            visited.add(url)
            resp = safe_get(client, url, throttle)
            if resp is None:
                break
            items = parse_list_html(resp.content, entry, str(resp.url), context)
            if not items:
                break
            yield items
            if page_size and len(items) < int(page_size):
                break  # son sayfa
            if mode == "none" or not arg:
                break
            if mode == "page_param":
                url = _with_query_param(first_url, arg, page_no)
                page_no += step
            elif mode == "path_template":
                url = arg.replace("{n}", str(page_no))
                page_no += step
            elif mode == "next_link":
                nxt = None
                try:
                    a = _soup(resp.content).select_one(arg)
                    nxt = a.get("href") if a else None
                except Exception:
                    nxt = None
                url = _abs_url(str(nxt), str(resp.url)) if nxt else None
            else:
                log.warning("%s: bilinmeyen sayfalama türü '%s'.", entry["name"], mode)
                break

    # -- detay -----------------------------------------------------------------

    def _fetch_details(self, companies: list[RawCompany], entry: dict[str, Any], client: httpx.Client, throttle: Throttle) -> None:
        limit = int(entry.get("max_detail_pages") or _DEFAULTS["max_detail_pages"])
        detail_kind = str(entry.get("detail_kind") or "html").lower()
        method = str(entry.get("detail_method") or "get").lower()
        url_tpl = entry.get("detail_url")
        headers = entry.get("detail_headers") or {}
        data_tpl = entry.get("detail_data") or {}
        detail_fields = list((entry.get("detail_json_keys") if detail_kind == "json" else entry.get("detail_selectors")) or {})
        fetched = 0
        for c in companies:
            if fetched >= limit:
                log.info("%s: detay sayfası üst sınırına (%d) ulaşıldı, kalan firmalar listeden gelen bilgiyle kaydedildi.", entry["name"], limit)
                break
            if not _needs_detail(c, detail_fields):
                continue  # detayın verebileceği her alan listeden zaten geldi
            ident = c.raw.get("detail_id")
            link = c.raw.get("detail_link")
            if url_tpl:
                if ("{id}" in url_tpl and not ident) or ("{detail_link}" in url_tpl and not link):
                    continue
                url = url_tpl.replace("{id}", str(ident or "")).replace("{detail_link}", str(link or ""))
            else:
                url = link
            if not url:
                continue
            fetched += 1
            if method == "post":
                form = {k: str(v).replace("{id}", str(ident or "")).replace("{detail_link}", str(link or "")) for k, v in data_tpl.items()}
                resp = self._safe_post(client, url, form, headers, throttle)
            else:
                resp = safe_get(client, url, throttle, headers=headers or None)
            if resp is None:
                continue
            try:
                if detail_kind == "json":
                    apply_detail_json(c, _loads_lenient(resp.text), entry)
                else:
                    apply_detail_html(c, resp.content, entry, str(resp.url))
            except Exception as exc:
                log.debug("%s: detay ayrıştırılamadı (%s): %s", entry["name"], url, exc)
        if fetched:
            log.info("%s: %d detay sayfası okundu.", entry["name"], fetched)

    @staticmethod
    def _safe_post(client: httpx.Client, url: str, data: dict[str, str], headers: dict[str, str], throttle: Throttle) -> httpx.Response | None:
        throttle.wait()
        try:
            r = client.post(url, data=data, headers=headers or None)
            if r.status_code >= 400:
                log.warning("HTTP %s: %s", r.status_code, url)
                return None
            return r
        except httpx.HTTPError as exc:
            log.warning("HTTP hatası %s: %s", url, exc)
            return None

    # -- yardımcılar -------------------------------------------------------------

    @staticmethod
    def _dedupe(companies: list[RawCompany], entry: dict[str, Any]) -> list[RawCompany]:
        """Aynı teknopark içinde adı aynı olan firmaları birleştirir (boş alanlar doldurulur)."""
        out: dict[str, RawCompany] = {}
        for c in companies:
            key = _dedupe_key(c.name)
            if key in out:
                kept = out[key]
                for field in ("website", "sector", "phone", "description", "address"):
                    if not getattr(kept, field) and getattr(c, field):
                        setattr(kept, field, getattr(c, field))
                for em in c.emails:
                    if em not in kept.emails:
                        kept.emails.append(em)
                for t in c.tags:
                    if t not in kept.tags:
                        kept.tags.append(t)
                # birden çok sektör sayfasında görülen firma: sektörleri etiket olarak sakla
                if c.sector and kept.sector and c.sector != kept.sector:
                    extra = kept.raw.setdefault("sectors", [kept.sector])
                    if c.sector not in extra:
                        extra.append(c.sector)
            else:
                out[key] = c
        dropped = len(companies) - len(out)
        if dropped:
            log.info("%s: %d yinelenen kayıt birleştirildi.", entry.get("name"), dropped)
        return list(out.values())

    def _robots_allowed(self, client: httpx.Client, url: str) -> bool:
        """Basit robots.txt kontrolü; dosya yoksa/okunamazsa izin verildiği varsayılır."""
        if not self.respect_robots:
            return True
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots_cache:
            rp: robotparser.RobotFileParser | None = None
            try:
                r = client.get(host + "/robots.txt")
                if r.status_code == 200 and "<html" not in r.text[:500].lower():
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError as exc:
                log.debug("robots.txt okunamadı (%s): %s", host, exc)
            self._robots_cache[host] = rp
        rp = self._robots_cache[host]
        if rp is None:
            return True
        try:
            ua = get_settings().http_user_agent
            return rp.can_fetch(ua, url) or rp.can_fetch("*", url)
        except Exception:
            return True


__all__ = [
    "TeknoparkCollector",
    "load_config",
    "list_technoparks",
    "select_entries",
    "parse_list_html",
    "parse_json",
    "extract_inline_json",
    "extract_sub_links",
    "apply_detail_html",
    "apply_detail_json",
    "DEFAULT_CONFIG_PATH",
    "SOURCE_NAME",
]
