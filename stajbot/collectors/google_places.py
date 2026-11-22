"""Google Places API (New) — Text Search toplayıcısı.

Hedef şehirlerdeki yazılım / bilişim / teknoloji şirketlerini Google Places API (New)
``places:searchText`` uç noktasından toplar ve ``RawCompany`` olarak üretir. Veritabanına
yazmaz; kayıt işi ``collectors.ingest`` modülünündür.

API anahtarı nasıl alınır ve kısıtlanır?
----------------------------------------
1. https://console.cloud.google.com adresinde bir proje oluşturun ve projeye bir
   **faturalandırma hesabı** bağlayın (Places API (New) ücretsiz kotası da faturalandırma
   hesabı ister; kredi kartı doğrulaması yapılır ama ücretsiz kota aşılmazsa ücret çıkmaz).
2. "API'ler ve Hizmetler > Kitaplık" bölümünden **Places API (New)** servisini etkinleştirin
   (``places.googleapis.com``). Eski "Places API" (legacy) DEĞİL; bu modül yalnızca yeni API ile çalışır.
3. "Google Maps Platform > Anahtarlar ve Kimlik Bilgileri > Kimlik bilgisi oluştur > API anahtarı".
4. Anahtarı kısıtlayın:
   - **API kısıtlamaları**: yalnızca "Places API (New)" seçin.
   - **Uygulama kısıtlamaları**: sunucuda çalışıyorsa "IP adresleri"; kendi bilgisayarınızda
     çalışan bir betik için kısıtlama bırakılabilir (anahtarı asla paylaşmayın / commit etmeyin).
5. Anahtarı proje kökündeki ``.env`` dosyasına yazın::

       GOOGLE_PLACES_API_KEY=AIza...

6. Önerilir: "Faturalandırma > Bütçeler ve uyarılar" bölümünden 5 $ gibi küçük bir bütçe uyarısı
   tanımlayın. Bu modül ayrıca ``data/google_places/request_counter.json`` dosyasında aylık istek
   sayacı tutar ve ücretsiz kota aşılırsa uyarı loglar.

gcloud ile alternatif::

    gcloud services enable places.googleapis.com
    gcloud services api-keys create --api-target=service=places.googleapis.com

Maliyet (fiyat sayfası, 2026-11-04):
-----------------------------------
Her sayfa isteği (``pageToken`` ile devam istekleri dahil) ayrı bir faturalanabilir olaydır ve
istek, alan maskesindeki EN PAHALI alanın kademesinden faturalanır.

- Text Search **Essentials** (sadece ``places.id``, ``places.name``, ``nextPageToken``): sınırsız ücretsiz.
- Text Search **Pro** (displayName, formattedAddress, addressComponents, types, businessStatus,
  googleMapsUri ...): ayda 5.000 olay ücretsiz, sonra 1.000 başına 32 $.
- Text Search **Enterprise** (``websiteUri``, telefon, rating, userRatingCount ...): ayda 1.000 olay
  ücretsiz, sonra 1.000 başına 35 $.
- Enterprise + Atmosphere (yorumlar, özetler — kullanılmaz): 1.000 ücretsiz, sonra 40 $/1.000.

Bu modül tek istekte web sitesi de aldığı için Enterprise kademesini kullanır:
10 şehir × 8 sorgu × 3 sayfa ≈ 240-270 istek/çalıştırma; ayda ~4 tam çalıştırma ücretsizdir,
sonrası çalıştırma başına yaklaşık 8-9 $. Ham yanıtlar ``data/google_places/`` altına önbelleğe
alınır; aynı sorgu 7 gün içinde yeniden çalıştırılırsa API'ye gidilmez (ücretsiz).

Not: Google kullanım koşullarına göre yer kimliği (place id) süresiz, diğer alanlar en çok 30 gün
saklanabilir; önbellek süresi bu yüzden 7 gün ile sınırlıdır.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from ..config import get_settings
from ..enums import TARGET_CITIES, ascii_fold, normalize_city
from ..log import get_logger
from ..utils import clean_text, domain_of, slugify
from .base import RawCompany, Throttle, make_http_client

log = get_logger(__name__)

SOURCE_NAME = "google_places"
API_URL = "https://places.googleapis.com/v1/places:searchText"

# Alan maskesi: ihtiyacımız olan alanlarla sınırlı (websiteUri/telefon/rating Enterprise kademesi).
FIELD_MASK = ",".join(
    [
        "nextPageToken",
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.types",
        "places.primaryType",
        "places.businessStatus",
        "places.googleMapsUri",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
    ]
)

# Her şehir için çalıştırılan Türkçe, kategorik sorgular (şehir adı eklenmez; konum
# kısıtlaması dikdörtgen ile verilir — metinde konum geçerse locationBias yok sayılır).
QUERY_TEMPLATES: list[str] = [
    "yazılım şirketi",
    "yazılım firması",
    "bilişim şirketi",
    "bilgi teknolojileri şirketi",
    "teknoloji şirketi",
    "ar-ge merkezi",
    "yazılım geliştirme",
    "mobil uygulama geliştirme",
]

# İsteğe bağlı ek sorgular (büyük şehirler için; varsayılan olarak çalışmaz).
EXTENDED_QUERY_TEMPLATES: list[str] = [
    "web yazılım ajansı",
    "dijital ajans yazılım",
    "yapay zeka şirketi",
    "siber güvenlik şirketi",
    "savunma sanayi yazılım",
    "otomasyon mühendislik yazılım",
    "gömülü sistem yazılım",
    "ERP yazılım",
    "oyun stüdyosu",
    "elektronik mühendislik şirketi",
    "bilgisayar mühendisliği danışmanlık",
]

# Şehir -> locationRestriction dikdörtgenleri ((low_lat, low_lng, high_lat, high_lng)).
# İstanbul, alaka doygunluğunu azaltmak için Avrupa / Anadolu olarak ikiye bölünmüştür.
CITY_RECTANGLES: dict[str, list[tuple[float, float, float, float]]] = {
    "Konya": [(37.70, 32.20, 38.15, 32.85)],
    "Ankara": [(39.70, 32.45, 40.15, 33.10)],
    "İstanbul": [(40.95, 28.50, 41.30, 29.05), (40.80, 29.00, 41.20, 29.60)],
    "İzmir": [(38.25, 26.90, 38.60, 27.45)],
    "Eskişehir": [(39.65, 30.30, 39.90, 30.75)],
    "Bursa": [(40.05, 28.80, 40.35, 29.35)],
    "Sakarya": [(40.65, 30.20, 40.90, 30.55)],
    "Antalya": [(36.75, 30.45, 37.05, 30.95)],
    "Mersin": [(36.70, 34.40, 36.95, 34.80)],
    "Afyonkarahisar": [(38.65, 30.40, 38.85, 30.70)],
}

# Posta kodu ön eki -> il (addressComponents yoksa son çare).
POSTAL_PREFIX_TO_CITY: dict[str, str] = {
    "42": "Konya",
    "06": "Ankara",
    "34": "İstanbul",
    "35": "İzmir",
    "26": "Eskişehir",
    "16": "Bursa",
    "54": "Sakarya",
    "07": "Antalya",
    "33": "Mersin",
    "03": "Afyonkarahisar",
}

# SKU fiyatları (USD / 1.000 olay) ve aylık ücretsiz olay sayıları — fiyat sayfası 2026-11-04.
SKU_PRICING: dict[str, dict[str, float | int | str]] = {
    "essentials": {"sku_id": "635D-A9DD-C520", "free_per_month": 10**12, "usd_per_1000": 0.0},
    "pro": {"sku_id": "4FDA-34B1-A910", "free_per_month": 5000, "usd_per_1000": 32.0},
    "enterprise": {"sku_id": "E967-44BC-B44D", "free_per_month": 1000, "usd_per_1000": 35.0},
    "enterprise_atmosphere": {"sku_id": "-", "free_per_month": 1000, "usd_per_1000": 40.0},
}
DEFAULT_TIER = "enterprise"  # FIELD_MASK websiteUri içerdiği için

# Ad bazlı gürültü filtresi (şirket olmayan sonuçlar). ASCII'ye indirgenmiş ada uygulanır.
_NOISE_RE = re.compile(
    r"\b("
    r"kurs|kursu|kurslari|dershane|dershanesi|etut|internet (cafe|kafe)|"
    r"bilgisayar (tamir|tamiri|servis|servisi|teknik servis)|"
    r"telefon (tamir|tamiri|servis|servisi)|telefoncu|cep telefonu|gsm bayi|"
    r"oyun salonu|playstation|ps5 cafe|kirtasiye|fotokopi|"
    r"bilgisayar kursu|surucu kursu|ozel ogretim|egitim merkezi"
    r")\b"
)

_SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com",
    "google.com", "goo.gl", "business.site", "wa.me", "whatsapp.com", "t.me", "tiktok.com",
)
# Önek ile eşleşenler (utm_source, utm_medium...) ve tam ad ile eşleşenler ayrı tutulur;
# "ref" öneki "reference" gibi meşru parametreleri silmesin.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "ref", "yclid", "msclkid"})

CLOSED_STATUSES = {"CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"}

# Teknopark / teknokent ipuçları (ASCII'ye indirgenmiş adres metninde aranır).
_TEKNOPARK_KEYWORDS = ("teknokent", "teknopark", "teknoloji gelistirme bolgesi", "cyberpark", "bilisim vadisi")

# Yalnızca harflerden oluşan il/ilçe adayı ("No:3/5" gibi kalıpları il sanmamak için).
_WORD_RE = re.compile(r"^[^\W\d_]+$")


# --- maliyet ------------------------------------------------------------------

def estimate_cost(n_requests: int, tier: str = DEFAULT_TIER, free_events_used: int = 0) -> float:
    """Verilen istek sayısının tahmini USD maliyeti.

    Her sayfa isteği bir olaydır; alan maskesindeki en pahalı alanın kademesi uygulanır.
    ``tier``: essentials | pro | enterprise | enterprise_atmosphere. ``free_events_used`` bu ay
    aynı kademede zaten harcanmış olay sayısıdır (aylık ücretsiz kota ondan düşülür).
    Örnek: ``estimate_cost(240)`` -> 0.0 (ilk 1.000 Enterprise olayı ücretsiz);
    ``estimate_cost(240, free_events_used=1000)`` -> 8.40.
    """
    if n_requests <= 0:
        return 0.0
    sku = SKU_PRICING.get(tier)
    if sku is None:
        raise ValueError(f"Bilinmeyen kademe: {tier}")
    free_left = max(0, int(sku["free_per_month"]) - max(0, free_events_used))
    billable = max(0, n_requests - free_left)
    return round(billable * float(sku["usd_per_1000"]) / 1000.0, 4)


# --- yanıt ayrıştırma -----------------------------------------------------------

def _component(components, type_name: str) -> str | None:
    if not isinstance(components, list):
        return None
    for c in components:
        if not isinstance(c, dict):
            continue
        types = c.get("types")
        if isinstance(types, list) and type_name in types:
            return clean_text(c.get("longText") or c.get("shortText"))
    return None


_ADDR_CITY_RE = re.compile(r"([^\s,/]+)\s*/\s*([^\s,/]+)")
# Posta kodu: 5 rakam, önünde/arkasında rakam ya da nokta yok ("10562. Sk." sokak numarası değil).
_POSTAL_RE = re.compile(r"(?<![\d.])(\d{5})(?![\d.])")


def _str_list(value) -> list[str]:
    """Liste değilse (ör. tek metin ya da None) güvenli listeye çevirir."""
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def detect_city_district(place: dict, queried_city: str | None) -> tuple[str | None, str | None]:
    """Yerin ilini ve ilçesini bulur.

    Sıra: addressComponents (administrative_area_level_1 = il, administrative_area_level_2 = ilçe);
    yoksa formattedAddress içindeki ``İlçe/İl`` kalıbı; yoksa posta kodu ön eki; yoksa sorgulanan şehir.
    Google Türkiye'de ``locality`` alanına çoğunlukla ilçeyi yazar, o yüzden il için kullanılmaz.
    """
    comps = place.get("addressComponents") or []
    city = _component(comps, "administrative_area_level_1")
    district = _component(comps, "administrative_area_level_2") or _component(comps, "locality")
    if city and district and ascii_fold(city) == ascii_fold(district):
        district = None

    addr = place.get("formattedAddress")
    addr = addr if isinstance(addr, str) else ""
    if not city and addr:
        # Sondan başa: "No:3/5" gibi sayı kalıplarını değil, harflerden oluşan "İlçe/İl" çiftini al.
        for d, c in reversed(_ADDR_CITY_RE.findall(addr)):
            if _WORD_RE.match(c) and _WORD_RE.match(d):
                city = c
                district = district or d
                break
    if not city and addr:
        for code in reversed(_POSTAL_RE.findall(addr)):
            if code[:2] in POSTAL_PREFIX_TO_CITY:
                city = POSTAL_PREFIX_TO_CITY[code[:2]]
                break
    if not city:
        city = queried_city
    return city, district


def _clean_website(url: str | None) -> tuple[str | None, str | None]:
    """Web sitesini normalize eder: izleme parametrelerini atar; sosyal/Google linklerini web sitesi saymaz.

    Döner: (website, linkedin_url).
    """
    if not url or not isinstance(url, str):
        return None, None
    u = url.strip()
    host = domain_of(u)
    if not host:
        return None, None
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return None, u
    for s in _SOCIAL_HOSTS:
        if host == s or host.endswith("." + s):
            return None, None
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    try:
        parts = urlparse(u)
        q = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not (k.lower() in _TRACKING_PARAMS or k.lower().startswith(_TRACKING_PREFIXES))
        ]
        parts = parts._replace(netloc=parts.netloc.lower(), query=urlencode(q), fragment="")
        u = urlunparse(parts)
    except ValueError:
        pass
    return u[:512], None


def is_noise_name(name: str | None) -> bool:
    """Ad, şirket olmayan bir yere (kurs, tamirci, internet kafe...) işaret ediyor mu?"""
    if not name:
        return True
    return bool(_NOISE_RE.search(ascii_fold(name)))


def place_to_raw(place: dict, queried_city: str | None, query: str | None = None) -> RawCompany | None:
    """Tek bir Place nesnesini RawCompany'ye çevirir. Atlanacaksa None döner."""
    if not isinstance(place, dict):
        return None
    status = place.get("businessStatus")
    if isinstance(status, str) and (status in CLOSED_STATUSES or status.startswith("CLOSED_")):
        return None
    # displayName normalde {"text": ..., "languageCode": ...}; bozuk yanıtta düz metin de olabilir.
    dn = place.get("displayName")
    name_text = dn.get("text") if isinstance(dn, dict) else dn
    name = clean_text(name_text if isinstance(name_text, str) else None, limit=255)
    if not name:
        return None
    if is_noise_name(name):
        return None

    city, district = detect_city_district(place, queried_city)
    city = clean_text(city, limit=64) if isinstance(city, str) else None
    district = clean_text(district, limit=64) if isinstance(district, str) else None
    website, linkedin = _clean_website(place.get("websiteUri"))
    address = clean_text(place.get("formattedAddress") if isinstance(place.get("formattedAddress"), str) else None)
    phone_raw = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber")
    phone = clean_text(phone_raw if isinstance(phone_raw, str) else None, limit=64)
    types = _str_list(place.get("types"))
    primary = place.get("primaryType")
    primary = primary if isinstance(primary, str) and primary.strip() else None
    tags = [primary] if primary else []
    addr_fold = ascii_fold(address or "")
    is_teknopark = any(k in addr_fold for k in _TEKNOPARK_KEYWORDS)
    teknopark_name = _teknopark_name_from_address(address) if is_teknopark else None

    maps_uri = place.get("googleMapsUri")
    maps_uri = maps_uri.strip() if isinstance(maps_uri, str) and maps_uri.strip() else None
    place_id = place.get("id")
    place_id = place_id if isinstance(place_id, str) and place_id else None

    raw = {
        "place_id": place_id,
        "types": types,
        "primary_type": primary,
        "business_status": status if isinstance(status, str) else None,
        "rating": place.get("rating") if isinstance(place.get("rating"), (int, float)) else None,
        "userRatingCount": place.get("userRatingCount") if isinstance(place.get("userRatingCount"), int) else None,
        "googleMapsUri": maps_uri,
        "queried_city": queried_city,
        "detected_city": city,
        "query": query,
    }
    return RawCompany(
        name=name,
        source=SOURCE_NAME,
        source_url=maps_uri,
        website=website,
        city=city,
        district=district,
        address=address,
        phone=phone,
        tags=tags,
        linkedin_url=linkedin,
        is_teknopark=is_teknopark,
        teknopark_name=teknopark_name,
        raw=raw,
    )


def _teknopark_name_from_address(address: str | None) -> str | None:
    """Adresin virgülle ayrılmış parçalarından teknokent/teknopark adını geçen parçayı döner."""
    if not address:
        return None
    for part in address.split(","):
        if any(k in ascii_fold(part) for k in _TEKNOPARK_KEYWORDS):
            return clean_text(part, limit=128)
    return None


def parse_response(data: dict, queried_city: str, query: str | None = None) -> list[RawCompany]:
    """Text Search yanıtını RawCompany listesine çevirir (çevrimdışı testler için).

    Kapalı işletmeler (``businessStatus`` CLOSED_*) ve gürültü adlar atlanır. Hata zarfı
    (``{"error": ...}``) gelirse boş liste döner.
    """
    if not isinstance(data, dict) or data.get("error"):
        return []
    places = data.get("places")
    if not isinstance(places, list):
        return []
    out: list[RawCompany] = []
    for place in places:
        if not isinstance(place, dict):
            continue
        try:
            rc = place_to_raw(place, queried_city, query)
        except Exception as e:  # bozuk tek kayıt tüm sayfayı düşürmesin
            log.warning("Google Places kaydı ayrıştırılamadı (%s): %s", place.get("id"), e)
            continue
        if rc is not None:
            out.append(rc)
    return out


# --- toplayıcı ----------------------------------------------------------------

def _rectangle_body(rect: tuple[float, float, float, float]) -> dict:
    low_lat, low_lng, high_lat, high_lng = rect
    return {
        "rectangle": {
            "low": {"latitude": low_lat, "longitude": low_lng},
            "high": {"latitude": high_lat, "longitude": high_lng},
        }
    }


class GooglePlacesCollector:
    """Google Places API (New) Text Search ile şehir × sorgu kombinasyonlarını tarar.

    - ``api_key`` yoksa ``settings.google_places_api_key`` kullanılır; o da yoksa Türkçe uyarı
      loglanır ve hiç kayıt üretilmez (exception yok).
    - Sayfalama ``nextPageToken`` ile en çok ``max_pages_per_query`` sayfa.
    - Aynı çalıştırma içinde yer kimliğine göre tekilleştirir.
    - 429 / 5xx için üstel bekleme ile yeniden dener; 401/403'te çalıştırmayı durdurur.
    - ``max_requests`` çalıştırma başına istek bütçesidir (ücretsiz kotayı korumak için).
    - Ham yanıtlar ``data_dir`` altına önbelleğe alınır (``use_cache``), aylık sayaç tutulur.
    """

    name = SOURCE_NAME

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        queries: list[str] | None = None,
        max_pages_per_query: int = 3,
        *,
        delay_sec: float = 0.3,
        max_requests: int = 500,
        max_retries: int = 3,
        data_dir: Path | None = None,
        use_cache: bool = True,
        cache_ttl_days: int = 7,
        page_size: int = 20,
    ):
        settings = get_settings()
        self.api_key = (api_key if api_key is not None else settings.google_places_api_key) or ""
        self._client = client
        self._owns_client = client is None
        self.queries = list(queries) if queries else list(QUERY_TEMPLATES)
        self.max_pages_per_query = max(1, min(int(max_pages_per_query), 3))
        self.throttle = Throttle(delay_sec)
        self.max_requests = max_requests
        self.max_retries = max_retries
        self.use_cache = use_cache
        self.cache_ttl = timedelta(days=cache_ttl_days)
        self.page_size = max(1, min(int(page_size), 20))
        self._data_dir = data_dir
        self.requests_made = 0  # bu çalıştırmada API'ye giden istek sayısı
        self.cache_hits = 0
        self.monthly_requests = 0  # request_counter.json'daki bu ayın toplamı (son bilinen)
        self._aborted = False

    # --- yardımcılar ---------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        if self._data_dir is None:
            self._data_dir = get_settings().abs_data_dir / "google_places"
        return self._data_dir

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }

    def _cache_path(self, city: str, query: str, rect_index: int, page: int) -> Path:
        return self.data_dir / slugify(city) / f"{slugify(query)}_r{rect_index}_p{page}.json"

    def _read_cache(self, path: Path) -> dict | None:
        if not self.use_cache:
            return None
        try:
            if not path.exists():
                return None
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age > self.cache_ttl:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError) as e:
            log.debug("Önbellek okunamadı %s: %s", path, e)
            return None

    def _write_cache(self, path: Path, data: dict) -> None:
        if not self.use_cache:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.debug("Önbellek yazılamadı %s: %s", path, e)

    def _bump_monthly_counter(self) -> int:
        """data/google_places/request_counter.json içindeki aylık sayacı artırır; yeni değeri döner."""
        path = self.data_dir / "request_counter.json"
        key = datetime.now().strftime("%Y-%m")
        counts: dict[str, int] = {}
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    counts = {str(k): int(v) for k, v in loaded.items() if isinstance(v, (int, float, str)) and str(v).lstrip("-").isdigit()}
        except (OSError, ValueError, TypeError):
            counts = {}
        counts[key] = max(0, int(counts.get(key, 0))) + 1
        self.monthly_requests = counts[key]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(counts, indent=1), encoding="utf-8")
        except OSError as e:
            log.debug("İstek sayacı yazılamadı: %s", e)
        return counts[key]

    def _post(self, client: httpx.Client, body: dict) -> dict | None:
        """Tek istek; 429/5xx için üstel bekleme ile yeniden dener. Hata -> None."""
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            self.throttle.wait()
            try:
                r = client.post(API_URL, json=body, headers=self._headers())
            except httpx.HTTPError as e:
                log.warning("Google Places isteği başarısız (%s): %s", body.get("textQuery"), e)
                if attempt >= self.max_retries:
                    return None
                time.sleep(backoff)
                backoff *= 2
                continue
            self.requests_made += 1
            monthly = self._bump_monthly_counter()
            free_cap = int(SKU_PRICING[DEFAULT_TIER]["free_per_month"])
            if monthly == free_cap + 1:
                log.warning(
                    "Google Places: bu ay %d istek yapıldı, ücretsiz Enterprise kotası (%d) aşıldı; "
                    "sonraki her istek ~%.3f $ ücretlendirilir.",
                    monthly, free_cap, float(SKU_PRICING[DEFAULT_TIER]["usd_per_1000"]) / 1000,
                )
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    log.warning("Google Places yanıtı JSON olarak çözülemedi (%s).", body.get("textQuery"))
                    return None
                if not isinstance(data, dict):
                    log.warning("Google Places yanıtı beklenen biçimde değil (%s): %s", body.get("textQuery"), type(data).__name__)
                    return None
                return data
            err = self._error_message(r)
            if r.status_code in (401, 403):
                log.error(
                    "Google Places erişim hatası HTTP %s: %s — API anahtarını, 'Places API (New)' "
                    "servisinin etkin olduğunu ve anahtar kısıtlamalarını kontrol edin. Toplama durduruldu.",
                    r.status_code, err,
                )
                self._aborted = True
                return None
            if r.status_code == 429 or r.status_code >= 500:
                if attempt >= self.max_retries:
                    log.warning("Google Places HTTP %s, yeniden deneme bitti (%s): %s", r.status_code, body.get("textQuery"), err)
                    return None
                log.info("Google Places HTTP %s, %.1f sn sonra yeniden deneniyor...", r.status_code, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            log.warning("Google Places HTTP %s (%s): %s", r.status_code, body.get("textQuery"), err)
            return None
        return None

    @staticmethod
    def _error_message(r: httpx.Response) -> str:
        try:
            payload = r.json()
            e = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(e, dict):
                return r.text[:200]
            return f"{e.get('status', '')} {e.get('message', '')}".strip() or r.text[:200]
        except (ValueError, AttributeError):
            return r.text[:200]

    def _base_body(self, query: str, rect: tuple[float, float, float, float] | None, city: str) -> dict:
        body: dict = {
            "textQuery": query if rect is not None else f"{query} {city}",
            "languageCode": "tr",
            "regionCode": "TR",
            "pageSize": self.page_size,
            "rankPreference": "RELEVANCE",
        }
        if rect is not None:
            body["locationRestriction"] = _rectangle_body(rect)
        return body

    def _iter_pages(
        self, client: httpx.Client, city: str, query: str, rect_index: int, rect: tuple[float, float, float, float] | None
    ) -> Iterator[dict]:
        """Bir şehir × sorgu (× dikdörtgen) için sayfaları üretir; önbellek varsa oradan okur."""
        body = self._base_body(query, rect, city)
        token: str | None = None
        for page in range(1, self.max_pages_per_query + 1):
            if token:
                body = dict(body, pageToken=token)
            cache_path = self._cache_path(city, query, rect_index, page)
            data = self._read_cache(cache_path)
            if data is not None:
                self.cache_hits += 1
            else:
                if self._aborted:
                    return
                if self.requests_made >= self.max_requests:
                    log.warning("Google Places: çalıştırma başına istek bütçesi (%d) doldu, kalan sorgular atlandı.", self.max_requests)
                    self._aborted = True
                    return
                data = self._post(client, body)
                if data is None:
                    return
                self._write_cache(cache_path, data)
            yield data
            token = data.get("nextPageToken") or None
            if not token:
                return

    # --- ana akış -------------------------------------------------------------

    def collect(self, cities: list[str] | None = None) -> Iterator[RawCompany]:
        if not self.api_key:
            log.warning(
                "GOOGLE_PLACES_API_KEY tanımlı değil; Google Places kaynağı atlandı. "
                "Anahtar almak için stajbot/collectors/google_places.py dosyasının başındaki açıklamaya bakın."
            )
            return
        raw_cities = list(cities) if cities else list(get_settings().target_cities or TARGET_CITIES)
        # Aynı şehrin farklı yazımları ("Konya", "konya", "istanbul") tek kez taranır (istek tasarrufu).
        canon_cities: list[str] = []
        for city in raw_cities:
            if not isinstance(city, str) or not city.strip():
                continue
            canon = normalize_city(city) or city.strip()
            if canon not in canon_cities:
                canon_cities.append(canon)
        self._aborted = False
        seen_ids: set[str] = set()
        client = self._client or make_http_client()
        produced = 0
        try:
            for canon in canon_cities:
                rects: list[tuple[float, float, float, float] | None] = list(CITY_RECTANGLES.get(canon, [])) or [None]
                if rects == [None]:
                    log.info("'%s' için konum dikdörtgeni yok; şehir adı sorgu metnine ekleniyor.", canon)
                city_count = 0
                for query in self.queries:
                    for ri, rect in enumerate(rects):
                        for data in self._iter_pages(client, canon, query, ri, rect):
                            for rc in parse_response(data, canon, query):
                                pid = rc.raw.get("place_id") or f"name:{ascii_fold(rc.name)}"
                                if pid in seen_ids:
                                    continue
                                seen_ids.add(pid)
                                city_count += 1
                                produced += 1
                                yield rc
                        if self._aborted:
                            break
                    if self._aborted:
                        break
                log.info("Google Places [%s]: %d yeni şirket (toplam istek: %d, önbellek: %d).", canon, city_count, self.requests_made, self.cache_hits)
                if self._aborted:
                    break
        finally:
            if self._owns_client:
                client.close()
            # Maliyet: bu ayın önceki istekleri ücretsiz kotadan düşülür.
            used_before = max(0, self.monthly_requests - self.requests_made)
            log.info(
                "Google Places tamamlandı: %d şirket, %d API isteği (tahmini ek maliyet %.2f $), %d önbellek isabeti.",
                produced, self.requests_made, estimate_cost(self.requests_made, free_events_used=used_before), self.cache_hits,
            )
