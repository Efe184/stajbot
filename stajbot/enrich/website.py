"""Şirket web sitesini tarar: e-postalar, teknolojiler, kariyer sayfası, LinkedIn, büyüklük ipucu.

Bkz. MIMARI.md bölüm 7. Ana sayfa + en çok 5 alt sayfa (iletişim / kariyer / hakkımızda ...), yalnızca
aynı alan adı, robots.txt'e uyar, sayfalar arası bekler. Sonuç ``Company`` üzerine yazılır (dolu alanlar
korunur), ``enriched_at`` güncellenir, ``Event(enrich)`` eklenir. Hiç exception fırlatmaz.

Alan adı kuralları:
- Ana sayfa başka bir alan adına yönlendiriyorsa yalnızca o iniş sayfası okunur, alt sayfalara gidilmez.
- Alt sayfa başka alan adına yönlendiriyorsa o sayfa yok sayılır.
- Şirketin kendi alan adındaki e-postalar önce gelir; kendi alan adında e-posta varsa yabancı (genel
  sağlayıcı olmayan) alan adlarındaki e-postalar atılır (teknopark, iş ortağı, araç sağlayıcı vb.).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlalchemy.orm import Session

from .. import services
from ..collectors.base import Throttle, make_http_client
from ..config import get_settings
from ..enums import Actor, EventType, SizeBucket, size_bucket_for
from ..log import get_logger
from ..models import Company
from ..utils import GENERIC_EMAIL_DOMAINS, clean_text, domain_of, is_valid_email, normalize_url, now_utc
from . import keywords

log = get_logger(__name__)

MAX_PAGES = 6
MAX_BYTES = 3_000_000
MAX_ROBOTS_BYTES = 512_000
MAX_EMAILS = 10

# Alt sayfa adayları: öncelik sırasıyla (ASCII'ye indirgenmiş yol ya da bağlantı metni üzerinde aranır)
_SUBPAGE_RULES: tuple[tuple[int, re.Pattern[str]], ...] = (
    (0, re.compile(r"kariyer|career|insan-?\s?kaynaklari|insankaynaklari|/ik(?:/|$|\.)|\bik\b|\bjobs?\b|basvuru|\bstaj\w*|\bintern(?:ship|s|ler|lik)?\b")),
    (1, re.compile(r"iletisim|contact|bize-?\s?ulasin|ulasim")),
    (2, re.compile(r"hakkimizda|hakkinda|about|kurumsal|biz-?\s?kimiz|ekibimiz|\bteam\b")),
)
_CAREER_RE = re.compile(
    r"kariyer|career|insan-?\s?kaynaklari|insankaynaklari|/ik(?:/|$|\.)|\bjobs?\b|is-?\s?ilanlari|acik-?\s?pozisyon"
    r"|\bis-?\s?basvuru|\bstaj-?\s?basvuru"
)
_INTERN_RE = re.compile(r"\bstaj\w*|\bintern(?:ship|s)?\b|\bstajyer", re.I)
_LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[^\s\"'<>?#&]+", re.I)
_LINKEDIN_TAIL_RE = re.compile(r"/(?:about|jobs|posts|people|life|mycompany|videos|images|events)(?:/.*)?$", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CFEMAIL_RE = re.compile(r"(?:data-cfemail=[\"']|/cdn-cgi/l/email-protection#)([0-9a-fA-F]+)")
_MAILTO_RE = re.compile(r"mailto:([^\"'<>\s?]+)", re.I)
_OBF_AT_RE = re.compile(r"\s*[\[\(\{]\s*(?:at|et)\s*[\]\)\}]\s*", re.I)
_OBF_DOT_RE = re.compile(r"\s*[\[\(\{]\s*(?:dot|nokta)\s*[\]\)\}]\s*", re.I)
_BAD_EMAIL_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "css", "js", "ico", "pdf", "woff", "woff2", "ttf", "otf", "eot", "mp4", "webm", "json", "map", "html", "htm", "php", "aspx"}
# Alan adı ya da alt alan adı bunlardan biriyse e-posta değildir (yer tutucu, izleme aracı, site altyapısı)
_BAD_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com", "yourdomain.com", "yourmail.com",
    "sentry.io", "wixpress.com", "wix.com", "mysite.com", "company.com", "site.com", "test.com", "sample.com",
    "ornek.com", "ornek.com.tr", "eposta.com", "e-posta.com", "adres.com",
    "schema.org", "w3.org", "googleapis.com", "gstatic.com", "cloudflare.com", "jsdelivr.net", "unpkg.com",
}
_BAD_LOCALS = {"noreply", "no-reply", "mailer-daemon", "postmaster", "abuse", "donotreply", "do-not-reply", "root", "webmaster", "hostmaster", "username", "kullanici", "kullaniciadi", "adiniz", "isim", "ad", "adsoyad", "ad.soyad", "name", "user", "you", "someone", "your", "name.surname", "firstname.lastname"}
_HEX_LOCAL_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)
_SIZE_RE = re.compile(
    r"(?<![\d.,/:%-])(?<!\d\s)(\d{1,3}(?:[.,]\d{3})+|\d{2,6})(?![\d.,]*\d)"
    r"\s*(?:\+|'?[uiu]?\s*(?:den|dan|ten|tan)\s+(?:fazla|cok)|'?(?:nin|nun|un|in|n)\s+(?:uzerinde|ustunde)|'?[uiu]?\s*(?:askin|asan)|'?den\s*cok|'?dan\s*cok|'?u\s*gecen|'?i\s*gecen)?"
    r"\s*(?:kisilik\s+)?(?:nin\s+|nun\s+)?(?:deneyimli\s+|uzman\s+|profesyonel\s+|genc\s+|dinamik\s+|yetkin\s+)?"
    r"(calisan|personel|muhendis|yazilimci|uzman|kisilik\s+(?:bir\s+)?(?:ekip|kadro|takim)|kisiden\s+olusan|kisilik|employees?|engineers?|professionals|team\s+members|people|developers|staff)",
)
_ADDRESS_BEFORE_RE = re.compile(r"\b(?:no|nu|numara|kat|daire|sok|sokak|sk|cad|cadde|cd|blok|kapi|apt|plaza|posta\s*kodu|pk|d|k)\W*$")
_CHARSET_HEADER_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", re.I)
_CHARSET_META_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.I)


@dataclass
class EnrichResult:
    ok: bool = True
    error: str | None = None
    url: str | None = None
    pages_fetched: int = 0
    emails: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    sector: str | None = None
    career_url: str | None = None
    has_internship_posting: bool = False
    linkedin_url: str | None = None
    employee_count: int | None = None
    new_contacts: int = 0
    # Ana sayfa başka bir alan adına yönlendirdiyse True (yalnızca iniş sayfası okunmuştur)
    landing_domain_differs: bool = False

    def summary(self) -> str:
        if not self.ok:
            return f"Zenginleştirme başarısız: {self.error}"
        parts = [f"{self.pages_fetched} sayfa", f"{len(self.emails)} e-posta", f"{len(self.tech_stack)} teknoloji"]
        if self.career_url:
            parts.append("kariyer sayfası" + (" (staj ilanı)" if self.has_internship_posting else ""))
        if self.employee_count:
            parts.append(f"~{self.employee_count} çalışan")
        if self.landing_domain_differs:
            parts.append("başka alan adına yönlendirdi")
        return "Zenginleştirildi: " + ", ".join(parts)


# --- ayrıştırma yardımcıları -----------------------------------------------------

def same_site(a: str | None, b: str | None) -> bool:
    """İki alan adı aynı siteye mi ait? (eşit ya da biri diğerinin alt alan adı)"""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def decode_cfemail(hexstr: str) -> str | None:
    """Cloudflare e-posta gizlemesini çözer (ilk bayt XOR anahtarı)."""
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def deobfuscate(text: str) -> str:
    text = _OBF_AT_RE.sub("@", text)
    text = _OBF_DOT_RE.sub(".", text)
    return text


def _email_ok(email: str) -> bool:
    if not is_valid_email(email):
        return False
    local, _, dom = email.rpartition("@")
    local_l = local.lower()
    dom_l = dom.lower()
    if dom_l.rsplit(".", 1)[-1] in _BAD_EMAIL_TLDS:
        return False
    if any(dom_l == bad or dom_l.endswith("." + bad) for bad in _BAD_EMAIL_DOMAINS):
        return False
    if local_l in _BAD_LOCALS:
        return False
    if "%" in local or len(email) > 254 or len(local) > 64:
        return False
    if re.search(r"@\d+x\.", email):  # logo@2x.png gibi görsel adları
        return False
    if _HEX_LOCAL_RE.match(local):  # sentry DSN, izleme anahtarları
        return False
    if re.fullmatch(r"[\d.-]+", dom_l.rsplit(".", 1)[0]):  # sürüm numarası görünümlü alan adı (x@1.2.3.js vb.)
        return False
    return True


def extract_emails(html: str, text: str | None = None) -> list[str]:
    """HTML (mailto, data-cfemail, düz metin, gizlenmiş yazım) içinden geçerli e-postaları çıkarır."""
    out: list[str] = []
    candidates: list[str] = []
    html = html or ""
    for hx in _CFEMAIL_RE.findall(html):
        dec = decode_cfemail(hx)
        if dec:
            candidates.append(dec)
    for raw in _MAILTO_RE.findall(html):
        try:
            candidates.append(unquote(raw))
        except Exception:  # noqa: BLE001
            continue
    blob = deobfuscate(html + "\n" + (text or ""))
    blob = re.sub(r"mailto:", " ", blob, flags=re.I)
    candidates.extend(_EMAIL_RE.findall(blob))
    for c in candidates:
        e = c.strip().strip(".,;:").lower()
        if _email_ok(e) and e not in out:
            out.append(e)
    return out


def prioritize_emails(emails: list[str], own_domains: list[str | None], limit: int = MAX_EMAILS) -> list[str]:
    """Şirketin kendi alan adındakiler önce, sonra genel sağlayıcılar (gmail vb.).

    Kendi alan adında e-posta varsa başka kurumsal alan adlarındakiler (teknopark, iş ortağı, araç
    sağlayıcı...) atılır; hiç yoksa son sırada tutulur.
    """
    owns = [d for d in own_domains if d]
    own: list[str] = []
    generic: list[str] = []
    foreign: list[str] = []
    for e in emails:
        d = e.rsplit("@", 1)[-1]
        if any(same_site(d, o) for o in owns):
            own.append(e)
        elif d in GENERIC_EMAIL_DOMAINS:
            generic.append(e)
        else:
            foreign.append(e)
    ranked = own + generic + (foreign if not own else [])
    return ranked[:limit]


def extract_links(tree: HTMLParser, base_url: str) -> list[tuple[str, str]]:
    """(mutlak url, bağlantı metni) listesi."""
    links: list[tuple[str, str]] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            absu = urljoin(base_url, href)
        except ValueError:
            continue
        if not absu.startswith(("http://", "https://")):
            continue
        links.append((absu.split("#", 1)[0], (a.text(strip=True) or "")[:80]))
    return links


def visible_text(tree: HTMLParser) -> str:
    """Görünen metin; script/style düğümlerini ağaçtan SİLER (önce ``tech_hints`` alınmalı)."""
    for tag in ("script", "style", "noscript", "svg", "template"):
        for n in tree.css(tag):
            n.decompose()
    body = tree.body or tree.root
    return body.text(separator=" ", strip=True) if body is not None else ""


def tech_hints(tree: HTMLParser) -> str:
    """Script/meta kaynaklarından teknoloji ipuçları (react, angular, wordpress...)."""
    bits: list[str] = []
    for n in tree.css("script[src]"):
        bits.append(n.attributes.get("src") or "")
    for n in tree.css("meta[name='generator'], meta[name='keywords'], meta[name='description'], meta[property='og:description']"):
        bits.append(n.attributes.get("content") or "")
    return " ".join(bits)


def _page_key(url: str) -> str:
    """Tekilleştirme anahtarı: şema/sorgu/parça yok, sondaki / yok, küçük harf."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{unquote(p.path).rstrip('/').lower()}"


def _link_haystack(url: str, text: str) -> str:
    return keywords.fold(f"{unquote(urlsplit(url).path)} {text}")


def pick_subpages(links: list[tuple[str, str]], base_url: str, limit: int = MAX_PAGES - 1) -> list[str]:
    base_dom = domain_of(base_url)
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = {_page_key(base_url)}
    for i, (url, text) in enumerate(links):
        if not same_site(domain_of(url), base_dom):
            continue
        path = urlsplit(url).path.lower()
        if path.rsplit(".", 1)[-1] in ("pdf", "jpg", "jpeg", "png", "gif", "zip", "rar", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "mp4"):
            continue
        hay = _link_haystack(url, text)
        for prio, rx in _SUBPAGE_RULES:
            if rx.search(hay):
                key = _page_key(url)
                if key not in seen:
                    seen.add(key)
                    scored.append((prio, i, url))
                break
    scored.sort()
    return [u for _, _, u in scored[: max(0, limit)]]


def find_career_url(links: list[tuple[str, str]], base_url: str) -> str | None:
    base_dom = domain_of(base_url)
    for url, text in links:
        if same_site(domain_of(url), base_dom) and _CAREER_RE.search(_link_haystack(url, text)):
            return url
    for url, text in links:  # kariyer.site.com, kariyer.net ilanı vb. dış bağlantılar
        if "linkedin.com" in url.lower():
            continue
        if _CAREER_RE.search(keywords.fold(f"{unquote(url)} {text}")):
            return url
    return None


def _clean_linkedin(url: str) -> str:
    url = url.rstrip("/")
    url = _LINKEDIN_TAIL_RE.sub("", url)
    return url.rstrip("/")


def find_linkedin(html: str, links: list[tuple[str, str]] | None = None) -> str | None:
    m = _LINKEDIN_RE.search(html or "")
    if m:
        return _clean_linkedin(m.group(0))
    for url, _ in links or []:
        m = _LINKEDIN_RE.match(url)
        if m:
            return _clean_linkedin(m.group(0))
    return None


def find_employee_count(text: str) -> int | None:
    folded = keywords.fold(text)
    best: int | None = None
    for m in _SIZE_RE.finditer(folded):
        if _ADDRESS_BEFORE_RE.search(folded[max(0, m.start() - 16): m.start()]):
            continue  # "No: 12 Uzman", "Kat 3 ..." gibi adres numaraları
        try:
            n = int(re.sub(r"[.,]", "", m.group(1)))
        except ValueError:
            continue
        if 5 <= n <= 200_000 and (best is None or n > best):
            best = n
    return best


# --- HTTP ---------------------------------------------------------------------

def _decode_body(content: bytes, ctype: str) -> str:
    """Gövdeyi metne çevirir: başlık charset'i → <meta charset> → UTF-8 → cp1254 (Türkçe eski siteler)."""
    enc: str | None = None
    m = _CHARSET_HEADER_RE.search(ctype or "")
    if m:
        enc = m.group(1)
    else:
        mm = _CHARSET_META_RE.search(content[:8192])
        if mm:
            enc = mm.group(1).decode("ascii", "ignore")
    if enc:
        try:
            return content.decode(enc, errors="replace")
        except LookupError:
            log.debug("Bilinmeyen karakter kümesi: %s", enc)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1254", errors="replace")


class _Robots:
    def __init__(self, client: httpx.Client, ua: str, throttle: Throttle | None = None):
        self.client = client
        self.ua = ua
        self.throttle = throttle
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return False
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._cache:
            rp: RobotFileParser | None = None
            try:
                if self.throttle:
                    self.throttle.wait()
                r = self.client.get(origin + "/robots.txt")
                ctype = (r.headers.get("content-type") or "").lower()
                if r.status_code == 200 and "html" not in ctype and len(r.content) <= MAX_ROBOTS_BYTES:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError as e:
                log.debug("robots.txt okunamadı %s: %s", origin, e)
            except Exception as e:  # noqa: BLE001 - robots okunamazsa izinli say
                log.debug("robots.txt işlenemedi %s: %s", origin, e)
            self._cache[origin] = rp
        rp = self._cache[origin]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.ua, url)
        except Exception:  # noqa: BLE001
            return True


def _fetch_html(client: httpx.Client, url: str, throttle: Throttle) -> tuple[str, str] | None:
    """(html, final_url) ya da None. Gövde akış olarak okunur; MAX_BYTES aşılırsa durulur."""
    throttle.wait()
    try:
        with client.stream("GET", url) as r:
            if 300 <= r.status_code < 400:  # istemci yönlendirme takip etmiyorsa
                log.debug("Yönlendirme takip edilmedi (%s): %s", r.status_code, url)
                return None
            if r.status_code >= 400:
                log.debug("HTTP %s: %s", r.status_code, url)
                return None
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype:
                return None
            cl = r.headers.get("content-length") or ""
            if cl.isdigit() and int(cl) > MAX_BYTES:
                log.debug("Sayfa çok büyük (%s bayt): %s", cl, url)
                return None
            chunks: list[bytes] = []
            size = 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    log.debug("Sayfa çok büyük (>%s bayt): %s", MAX_BYTES, url)
                    return None
                chunks.append(chunk)
            final_url = str(r.url)
    except httpx.HTTPError as e:
        log.debug("HTTP hatası %s: %s", url, e)
        return None
    content = b"".join(chunks)
    if content[:5] == b"%PDF-":
        return None
    return _decode_body(content, ctype), final_url


def _alt_scheme(url: str) -> str | None:
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return None


def crawl(start_url: str, client: httpx.Client, *, throttle: Throttle | None = None, max_pages: int = MAX_PAGES, respect_robots: bool = True) -> EnrichResult:
    """Siteyi tarar ve bulguları döner (veritabanına dokunmaz)."""
    settings = get_settings()
    throttle = throttle or Throttle(max(1.0, settings.crawl_delay_sec))
    res = EnrichResult(url=start_url)
    start_url = normalize_url(start_url) or start_url
    start_dom = domain_of(start_url)
    if not start_dom:
        res.ok, res.error = False, "Geçersiz web adresi"
        return res
    robots = _Robots(client, settings.http_user_agent, throttle) if respect_robots else None

    if robots and not robots.allowed(start_url):
        res.ok, res.error = False, "robots.txt taramaya izin vermiyor"
        return res
    home = _fetch_html(client, start_url, throttle)
    if home is None:
        alt = _alt_scheme(start_url)
        if alt and (not robots or robots.allowed(alt)):
            home = _fetch_html(client, alt, throttle)
    if home is None:
        res.ok, res.error = False, "Siteye ulaşılamadı"
        return res
    html, final_url = home
    res.url = final_url
    res.pages_fetched = 1
    final_dom = domain_of(final_url)
    res.landing_domain_differs = not same_site(start_dom, final_dom)
    visited: set[str] = {_page_key(start_url), _page_key(final_url)}

    tree = HTMLParser(html)
    links = extract_links(tree, final_url)
    hints = [tech_hints(tree)]  # visible_text script düğümlerini sildiği için önce
    text = visible_text(tree)
    texts = [text]
    htmls = [html]

    res.career_url = find_career_url(links, final_url)
    res.linkedin_url = find_linkedin(html, links)
    career_text = ""
    sub: list[str] = []
    if res.landing_domain_differs:
        log.info("Site başka alan adına yönlendirdi (%s → %s); yalnızca iniş sayfası okundu", start_dom, final_dom)
    else:
        sub = pick_subpages(links, final_url, limit=max_pages - 1)
        if res.career_url and same_site(domain_of(res.career_url), final_dom) and _page_key(res.career_url) not in {_page_key(u) for u in sub} and _page_key(res.career_url) not in visited:
            sub.insert(0, res.career_url)
    for url in sub[: max(0, max_pages - 1)]:
        if _page_key(url) in visited:
            continue
        visited.add(_page_key(url))
        if robots and not robots.allowed(url):
            continue
        page = _fetch_html(client, url, throttle)
        if page is None:
            continue
        phtml, purl = page
        if not same_site(domain_of(purl), final_dom):
            log.debug("Alt sayfa başka alan adına yönlendirdi, atlandı: %s -> %s", url, purl)
            continue
        if _page_key(purl) in visited and _page_key(purl) != _page_key(url):
            continue  # aynı sayfaya geri yönlendirme
        visited.add(_page_key(purl))
        res.pages_fetched += 1
        ptree = HTMLParser(phtml)
        hints.append(tech_hints(ptree))
        ptext = visible_text(ptree)
        texts.append(ptext)
        htmls.append(phtml)
        if res.career_url is None:
            res.career_url = find_career_url(extract_links(ptree, purl), purl)
        if res.linkedin_url is None:
            res.linkedin_url = find_linkedin(phtml, extract_links(ptree, purl))
        if res.career_url and _page_key(res.career_url) in (_page_key(url), _page_key(purl)):
            career_text = ptext
    all_text = "\n".join(texts)
    found: list[str] = []
    for h, t in zip(htmls, texts):
        for e in extract_emails(h, t):
            if e not in found:
                found.append(e)
    res.emails = prioritize_emails(found, [start_dom, final_dom])
    res.tech_stack = keywords.detect_technologies(all_text + " " + " ".join(hints))
    res.sector = keywords.detect_sector(all_text)
    if career_text:
        res.has_internship_posting = bool(_INTERN_RE.search(career_text))
    elif res.career_url and _INTERN_RE.search(" ".join(t for u, t in links if u == res.career_url)):
        res.has_internship_posting = True
    res.employee_count = find_employee_count(all_text)
    return res


# --- veritabanına yazma ---------------------------------------------------------

def start_url_for(company: Company) -> str | None:
    if company.website:
        u = normalize_url(company.website)
        if u:
            return u
    if company.domain:
        return f"https://{company.domain}"
    return None


def apply_result(company: Company, res: EnrichResult, session: Session) -> int:
    """Bulguları şirkete yazar (dolu alanlar korunur). Eklenen yeni kişi sayısını döner."""
    before = {c.email for c in company.contacts}
    for e in res.emails:
        services.add_contact(session, company, e, source="website")
    new_contacts = len({c.email for c in company.contacts} - before)
    res.new_contacts = new_contacts
    stack = list(company.tech_stack or [])
    for t in res.tech_stack:
        if t not in stack:
            stack.append(t)
    if stack != (company.tech_stack or []):
        company.tech_stack = stack
    if res.sector and not company.sector:
        company.sector = res.sector
    if res.career_url and not company.career_url:
        company.career_url = res.career_url[:512]
    if res.has_internship_posting:
        company.has_internship_posting = True
        if not company.internship_posting_url and res.career_url:
            company.internship_posting_url = res.career_url[:512]
    if res.linkedin_url and not company.linkedin_url:
        company.linkedin_url = res.linkedin_url[:512]
    if res.employee_count and not company.employee_count:
        company.employee_count = res.employee_count
        if company.size_bucket in (None, SizeBucket.unknown):
            company.size_bucket = size_bucket_for(res.employee_count)
    res_dom = domain_of(res.url) if res.url else None
    # Başka alan adına yönlenen iniş sayfası şirketin adresi/alan adı olarak yazılmaz
    if res.url and not company.website and not res.landing_domain_differs:
        if not company.domain or same_site(res_dom, company.domain):
            company.website = res.url[:512]
    if not company.domain and res_dom and not res.landing_domain_differs:
        from sqlalchemy import select

        taken = session.scalar(select(Company.id).where(Company.domain == res_dom, Company.id != company.id))
        if taken is None:
            company.domain = res_dom
    return new_contacts


def enrich_company(company: Company, session: Session, client: httpx.Client | None = None) -> EnrichResult:
    """Şirketin sitesini tarar ve sonuçları kaydeder; ``enriched_at`` her durumda güncellenir."""
    url = start_url_for(company)
    if not url:
        res = EnrichResult(ok=False, error="Web sitesi yok")
        _finish(company, res, session)
        return res
    own_client = client is None
    client = client or make_http_client()
    try:
        res = crawl(url, client)
    except Exception as e:  # noqa: BLE001 - zenginleştirme asla exception fırlatmaz
        log.warning("Zenginleştirme hatası [%s] %s: %s", company.id, url, e, exc_info=log.isEnabledFor(10))
        res = EnrichResult(ok=False, error=f"{type(e).__name__}: {e}", url=url)
    finally:
        if own_client:
            client.close()
    try:
        if res.ok:
            apply_result(company, res, session)
    except Exception as e:  # noqa: BLE001
        log.warning("Zenginleştirme sonucu yazılamadı [%s]: %s", company.id, e, exc_info=log.isEnabledFor(10))
        res.ok, res.error = False, f"Sonuç yazılamadı: {type(e).__name__}: {e}"
    _finish(company, res, session)
    log.info("[%s] %s — %s", company.id, company.name, res.summary())
    return res


def _finish(company: Company, res: EnrichResult, session: Session) -> None:
    company.enriched_at = now_utc()
    company.updated_at = now_utc()
    body_parts = []
    if res.emails:
        body_parts.append("E-posta: " + ", ".join(res.emails))
    if res.tech_stack:
        body_parts.append("Teknoloji: " + ", ".join(res.tech_stack))
    if res.career_url:
        body_parts.append("Kariyer: " + res.career_url)
    if res.landing_domain_differs and res.url:
        body_parts.append("Yönlendirme: " + res.url)
    if res.error:
        body_parts.append("Hata: " + res.error)
    services.add_event(session, company, EventType.enrich, clean_text(res.summary(), 255) or "Zenginleştirme", body="\n".join(body_parts) or None, actor=Actor.system)
    services.maybe_mark_ready(session, company)
    session.flush()
