"""Büyük şirket / dernek üye listeleri toplayıcıları ("bilisim500" kaynağı).

Bilişim 500 sıralaması ücretli bir PDF olarak satılır (BThaber), serbestçe erişilebilir
bir liste yoktur. Bu yüzden bu modül üç kaynağı kapsar:

* ``YasadCollector``   (name="yasad")     — YASAD üye/katalog listesi. Birincil yol WordPress
  REST API (``/wp-json/wp/v2/firmalar``); API kapatılırsa HTML liste + detay sayfaları kullanılır.
* ``TubisadCollector`` (name="tubisad")   — TÜBİSAD Kurumsal Üye Listesi (statik HTML tablo;
  yalnızca web sitesi ve logo adı vardır, şehir yoktur).
* ``Bilisim500Collector`` (name="bilisim500") — Kullanıcı Bilişim 500 PDF'ini satın alıp
  ``data/manual/bilisim500_<yil>.csv`` olarak dışa aktarırsa onu okur; dosya yoksa boş döner.

Tümü ``SOURCES`` listesinde dışa aktarılır; ``AssociationCollectors.collect(cities)`` hepsini
zincirler. Çevrimdışı kullanılabilen ``parse_*`` fonksiyonları ``tests/fixtures/bilisim500/``
fikstürleriyle test edilir.

Şehir bilgisi: YASAD'da ``lokasyon`` taksonomisi ya da adres metninden çıkarılır; TÜBİSAD'da
hiç yoktur. Şehri bulunamayan kayıtlar ``city=None`` ve ``"sehir-bilinmiyor"`` etiketiyle
üretilir; ingest bunları atlar, sonraki web sitesi zenginleştirmesi şehri bulabilir.
"""
from __future__ import annotations

import csv
import html
import io
import re
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.lexbor import LexborHTMLParser

from ..config import get_settings
from ..enums import ascii_fold, is_target_city, normalize_city
from ..log import get_logger
from ..utils import clean_text, domain_of, is_valid_email
from .base import RawCompany, Throttle, make_http_client, safe_get

log = get_logger(__name__)

__all__ = [
    "SOURCES",
    "SOURCE_NAMES",
    "AssociationCollectors",
    "YasadCollector",
    "TubisadCollector",
    "Bilisim500Collector",
    "CityGuess",
    "collect",
    "get_collector",
    "parse_yasad_rest",
    "parse_yasad_listing",
    "parse_yasad_detail",
    "parse_tubisad",
    "parse_bilisim500_csv",
    "city_from_text",
    "resolve_city",
    "normalize_website",
    "site_origin",
    "term_map",
    "filter_by_cities",
]

# --- sabitler ------------------------------------------------------------------

SOURCE_YASAD = "yasad"
SOURCE_TUBISAD = "tubisad"
SOURCE_BILISIM500 = "bilisim500"

YASAD_BASE = "https://www.yasad.org.tr"
YASAD_REST_URL = f"{YASAD_BASE}/wp-json/wp/v2/firmalar"
YASAD_TAX_URL = f"{YASAD_BASE}/wp-json/wp/v2/{{tax}}"
YASAD_LIST_URL = f"{YASAD_BASE}/uyelerimiz/"
YASAD_REST_FIELDS = "id,slug,link,title,acf,lokasyon,sektor-kategorisi,firma-durum"
YASAD_PER_PAGE = 100
YASAD_MAX_REST_PAGES = 50
YASAD_MAX_HTML_PAGES = 40

# firma-durum taksonomisi
YASAD_TERM_MEMBER = {151, 208}   # "Üyeler", "üye"
YASAD_TERM_CATALOG = {149, 209}  # "Katalog", "katalo"

TUBISAD_URL = "https://www.tubisad.org.tr/tr/uyelik/detay/Uye-Listesi/213/4206/0"

UNKNOWN_CITY_TAG = "sehir-bilinmiyor"
GUESSED_NAME_TAG = "ad-tahmini"

# REST taksonomi çağrısı başarısız olursa kullanılan yedek haritalar (2026-11-04 itibarıyla).
YASAD_LOKASYON_FALLBACK: dict[int, str] = {
    47: "Adana", 48: "Adıyaman", 49: "Afyonkarahisar", 50: "Ağrı", 114: "Aksaray", 51: "Amasya",
    52: "Ankara", 53: "Antalya", 121: "Ardahan", 54: "Artvin", 55: "Aydın", 56: "Balıkesir",
    120: "Bartın", 118: "Batman", 115: "Bayburt", 57: "Bilecik", 58: "Bingöl", 59: "Bitlis",
    60: "Bolu", 61: "Burdur", 62: "Bursa", 63: "Çanakkale", 64: "Çankırı", 65: "Çorum",
    66: "Denizli", 67: "Diyarbakır", 127: "Düzce", 68: "Edirne", 69: "Elazığ", 70: "Erzincan",
    71: "Erzurum", 72: "Eskişehir", 73: "Gaziantep", 210: "GEBZE", 74: "Giresun",
    75: "Gümüşhane", 76: "Hakkari", 77: "Hatay", 122: "Iğdır", 78: "Isparta", 80: "İstanbul",
    81: "İzmir", 92: "Kahramanmaraş", 124: "Karabük", 116: "Karaman", 82: "Kars",
    83: "Kastamonu", 84: "Kayseri", 125: "Kilis", 117: "Kırıkkale", 85: "Kırklareli",
    86: "Kırşehir", 87: "Kocaeli", 88: "Konya", 89: "Kütahya", 90: "Malatya", 91: "Manisa",
    93: "Mardin", 79: "Mersin", 94: "Muğla", 95: "Muş", 96: "Nevşehir", 97: "Niğde", 98: "Ordu",
    126: "Osmaniye", 99: "Rize", 100: "Sakarya", 101: "Samsun", 109: "Şanlıurfa", 102: "Siirt",
    103: "Sinop", 104: "Sivas", 119: "Şırnak", 105: "Tekirdağ", 106: "Tokat", 107: "Trabzon",
    108: "Tunceli", 110: "Uşak", 111: "Van", 123: "Yalova", 112: "Yozgat", 113: "Zonguldak",
}

YASAD_SEKTOR_FALLBACK: dict[int, str] = {
    131: "Akıllı Şehir Teknolojileri", 213: "Backend", 202: "Bankacılık", 204: "Bankacılık",
    133: "Bilişim Hizmetleri Yazılımları", 137: "Coğrafi Bilgi Sistemleri",
    143: "Depo Yönetimi ve İzlenebilirlik", 136: "Dijitalleşme Araçları", 139: "E-Devlet Yazılımları",
    142: "Eğitim Yazılımları", 132: "ERP & PLM", 211: "Frontend", 199: "İnsan Kaynakları",
    138: "İş Akışı ve Doküman Yönetimi", 144: "İş Zekası ve Analitik Çözümler",
    134: "Kritik / Yeni Teknolojiler", 212: "Mobil", 147: "Oyun ve Eğlence Yazılımları",
    146: "Platform Yazılımları", 128: "Sektörel Yazılım Şirketi",
    205: "Sektörel Yazılım Şirketi 86 Bankacılık", 140: "Siber Güvenlik Yazılımları",
    145: "Telekom Yazılımları", 135: "Temel İş Uygulamaları", 203: "Tüm Sektörler",
    141: "Üretim Yönetim Sistemleri - MES", 201: "Ürün ve Ekip Saha Yönetimi",
    148: "Videokonferans ve İşbirliği Çözümleri",
}

# --- şehir çıkarımı ---------------------------------------------------------------

PROVINCES: tuple[str, ...] = (
    "Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Aksaray", "Amasya", "Ankara", "Antalya",
    "Ardahan", "Artvin", "Aydın", "Balıkesir", "Bartın", "Batman", "Bayburt", "Bilecik", "Bingöl",
    "Bitlis", "Bolu", "Burdur", "Bursa", "Çanakkale", "Çankırı", "Çorum", "Denizli", "Diyarbakır",
    "Düzce", "Edirne", "Elazığ", "Erzincan", "Erzurum", "Eskişehir", "Gaziantep", "Giresun",
    "Gümüşhane", "Hakkari", "Hatay", "Iğdır", "Isparta", "İstanbul", "İzmir", "Kahramanmaraş",
    "Karabük", "Karaman", "Kars", "Kastamonu", "Kayseri", "Kilis", "Kırıkkale", "Kırklareli",
    "Kırşehir", "Kocaeli", "Konya", "Kütahya", "Malatya", "Manisa", "Mardin", "Mersin", "Muğla",
    "Muş", "Nevşehir", "Niğde", "Ordu", "Osmaniye", "Rize", "Sakarya", "Samsun", "Şanlıurfa",
    "Siirt", "Sinop", "Sivas", "Şırnak", "Tekirdağ", "Tokat", "Trabzon", "Tunceli", "Uşak", "Van",
    "Yalova", "Yozgat", "Zonguldak",
)

# Eski/kısa yazımlar -> il
_PROVINCE_ALIASES: dict[str, str] = {
    "afyon": "Afyonkarahisar", "icel": "Mersin", "urfa": "Şanlıurfa", "maras": "Kahramanmaraş",
    "antep": "Gaziantep", "izmit": "Kocaeli",
}

_PROVINCE_BY_KEY: dict[str, str] = {ascii_fold(p): p for p in PROVINCES}
_PROVINCE_BY_KEY.update(_PROVINCE_ALIASES)

# "Ankara Caddesi", "İzmir Yolu" gibi il adlı sokak/yol ifadeleri il sayılmaz.
_STREET_WORDS = r"(?:cad|cd|cadde|caddesi|sok|sk|sokak|sokagi|mah|mh|mahalle|mahallesi|blv|bulv|bulvar|bulvari|yolu|yol|kopru|koprusu)"
_PROVINCE_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in _PROVINCE_BY_KEY), key=len, reverse=True)) + r")\b"
    r"(?!\s*\.?\s*" + _STREET_WORDS + r"\b)"
)

# ilçe / bölge / teknokent adı (ASCII, küçük harf) -> (görünen ad, il)
_DISTRICTS: dict[str, tuple[str, str]] = {
    # İstanbul
    "sariyer": ("Sarıyer", "İstanbul"), "maslak": ("Sarıyer", "İstanbul"), "esenler": ("Esenler", "İstanbul"),
    "atasehir": ("Ataşehir", "İstanbul"), "icerenkoy": ("Ataşehir", "İstanbul"), "besiktas": ("Beşiktaş", "İstanbul"),
    "levent": ("Beşiktaş", "İstanbul"), "etiler": ("Beşiktaş", "İstanbul"), "kadikoy": ("Kadıköy", "İstanbul"),
    "umraniye": ("Ümraniye", "İstanbul"), "sisli": ("Şişli", "İstanbul"), "mecidiyekoy": ("Şişli", "İstanbul"),
    "bakirkoy": ("Bakırköy", "İstanbul"), "pendik": ("Pendik", "İstanbul"), "beykoz": ("Beykoz", "İstanbul"),
    "kartal": ("Kartal", "İstanbul"), "tuzla": ("Tuzla", "İstanbul"), "maltepe": ("Maltepe", "İstanbul"),
    "uskudar": ("Üsküdar", "İstanbul"), "beyoglu": ("Beyoğlu", "İstanbul"), "fatih": ("Fatih", "İstanbul"),
    "kagithane": ("Kağıthane", "İstanbul"), "bagcilar": ("Bağcılar", "İstanbul"), "basaksehir": ("Başakşehir", "İstanbul"),
    "bahcelievler": ("Bahçelievler", "İstanbul"), "zeytinburnu": ("Zeytinburnu", "İstanbul"), "avcilar": ("Avcılar", "İstanbul"),
    "esenyurt": ("Esenyurt", "İstanbul"), "beylikduzu": ("Beylikdüzü", "İstanbul"), "buyukcekmece": ("Büyükçekmece", "İstanbul"),
    "kucukcekmece": ("Küçükçekmece", "İstanbul"), "sancaktepe": ("Sancaktepe", "İstanbul"), "cekmekoy": ("Çekmeköy", "İstanbul"),
    "gaziosmanpasa": ("Gaziosmanpaşa", "İstanbul"), "eyupsultan": ("Eyüpsultan", "İstanbul"), "bayrampasa": ("Bayrampaşa", "İstanbul"),
    "gungoren": ("Güngören", "İstanbul"), "sultanbeyli": ("Sultanbeyli", "İstanbul"), "arnavutkoy": ("Arnavutköy", "İstanbul"),
    "teknopark istanbul": ("Pendik", "İstanbul"), "itu ari": ("Sarıyer", "İstanbul"), "ari teknokent": ("Sarıyer", "İstanbul"),
    "yildiz teknopark": ("Esenler", "İstanbul"), "ytu": ("Esenler", "İstanbul"), "bogazici universitesi": ("Sarıyer", "İstanbul"),
    # Ankara
    "cankaya": ("Çankaya", "Ankara"), "yenimahalle": ("Yenimahalle", "Ankara"), "golbasi": ("Gölbaşı", "Ankara"),
    "etimesgut": ("Etimesgut", "Ankara"), "sincan": ("Sincan", "Ankara"), "kecioren": ("Keçiören", "Ankara"),
    "mamak": ("Mamak", "Ankara"), "altindag": ("Altındağ", "Ankara"), "pursaklar": ("Pursaklar", "Ankara"),
    "bilkent": ("Çankaya", "Ankara"), "cyberpark": ("Çankaya", "Ankara"), "odtu": ("Çankaya", "Ankara"),
    "hacettepe": ("Çankaya", "Ankara"), "beytepe": ("Çankaya", "Ankara"), "ostim": ("Yenimahalle", "Ankara"),
    "ivedik": ("Yenimahalle", "Ankara"), "kizilay": ("Çankaya", "Ankara"), "cukurambar": ("Çankaya", "Ankara"),
    "sogutozu": ("Çankaya", "Ankara"), "balgat": ("Çankaya", "Ankara"), "umitkoy": ("Çankaya", "Ankara"),
    "cayyolu": ("Çankaya", "Ankara"), "gazi teknopark": ("Yenimahalle", "Ankara"),
    # İzmir
    "bornova": ("Bornova", "İzmir"), "konak": ("Konak", "İzmir"), "karsiyaka": ("Karşıyaka", "İzmir"),
    "bayrakli": ("Bayraklı", "İzmir"), "urla": ("Urla", "İzmir"), "buca": ("Buca", "İzmir"), "gaziemir": ("Gaziemir", "İzmir"),
    "cigli": ("Çiğli", "İzmir"), "narlidere": ("Narlıdere", "İzmir"), "balcova": ("Balçova", "İzmir"),
    "karabaglar": ("Karabağlar", "İzmir"), "alsancak": ("Konak", "İzmir"), "iyte": ("Urla", "İzmir"),
    "depark": ("Buca", "İzmir"), "teknopark izmir": ("Urla", "İzmir"),
    # Bursa
    "nilufer": ("Nilüfer", "Bursa"), "osmangazi": ("Osmangazi", "Bursa"), "yildirim": ("Yıldırım", "Bursa"),
    "gursu": ("Gürsu", "Bursa"), "kestel": ("Kestel", "Bursa"), "mudanya": ("Mudanya", "Bursa"), "inegol": ("İnegöl", "Bursa"),
    "ulutek": ("Nilüfer", "Bursa"), "gorukle": ("Nilüfer", "Bursa"),
    # Konya
    "selcuklu": ("Selçuklu", "Konya"), "meram": ("Meram", "Konya"), "karatay": ("Karatay", "Konya"),
    "innopark": ("Selçuklu", "Konya"),
    # Eskişehir
    "tepebasi": ("Tepebaşı", "Eskişehir"), "odunpazari": ("Odunpazarı", "Eskişehir"), "etgb": ("Odunpazarı", "Eskişehir"),
    # Sakarya
    "serdivan": ("Serdivan", "Sakarya"), "adapazari": ("Adapazarı", "Sakarya"), "erenler": ("Erenler", "Sakarya"),
    "hendek": ("Hendek", "Sakarya"), "arifiye": ("Arifiye", "Sakarya"),
    # Antalya
    "muratpasa": ("Muratpaşa", "Antalya"), "konyaalti": ("Konyaaltı", "Antalya"), "kepez": ("Kepez", "Antalya"),
    "dosemealti": ("Döşemealtı", "Antalya"), "alanya": ("Alanya", "Antalya"), "manavgat": ("Manavgat", "Antalya"),
    # Mersin
    "yenisehir": ("Yenişehir", "Mersin"), "mezitli": ("Mezitli", "Mersin"), "toroslar": ("Toroslar", "Mersin"),
    "tarsus": ("Tarsus", "Mersin"), "erdemli": ("Erdemli", "Mersin"), "silifke": ("Silifke", "Mersin"),
    # Afyonkarahisar
    "sandikli": ("Sandıklı", "Afyonkarahisar"), "bolvadin": ("Bolvadin", "Afyonkarahisar"), "dinar": ("Dinar", "Afyonkarahisar"),
    # Hedef dışı ama doğru etiketlensin
    "gebze": ("Gebze", "Kocaeli"), "bilisim vadisi": ("Gebze", "Kocaeli"), "muallimkoy": ("Gebze", "Kocaeli"),
    "basiskele": ("Başiskele", "Kocaeli"), "cayirova": ("Çayırova", "Kocaeli"), "darica": ("Darıca", "Kocaeli"),
    "kocasinan": ("Kocasinan", "Kayseri"), "melikgazi": ("Melikgazi", "Kayseri"), "talas": ("Talas", "Kayseri"),
    "seyhan": ("Seyhan", "Adana"), "yuregir": ("Yüreğir", "Adana"), "cukurova": ("Çukurova", "Adana"),
    "pamukkale": ("Pamukkale", "Denizli"), "merkezefendi": ("Merkezefendi", "Denizli"),
    "atakum": ("Atakum", "Samsun"), "ilkadim": ("İlkadım", "Samsun"),
    "sehitkamil": ("Şehitkamil", "Gaziantep"), "sahinbey": ("Şahinbey", "Gaziantep"),
    "ortahisar": ("Ortahisar", "Trabzon"), "yesilyurt": ("Yeşilyurt", "Malatya"), "yunusemre": ("Yunusemre", "Manisa"),
}
# "Fatih Mah.", "Kartal Sok." gibi mahalle/sokak adları ilçe sayılmaz (her şehirde Fatih Mahallesi var).
_DISTRICT_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in _DISTRICTS), key=len, reverse=True)) + r")\b"
    r"(?!\s*\.?\s*" + _STREET_WORDS + r"\b)"
)

# Teknopark/teknokent ipuçları (ASCII küçük harf metin üzerinde)
_TEKNOPARK_RE = re.compile(
    r"teknokent|teknopark|tekno park|cyberpark|bilisim vadisi|teknoloji gelistirme|\btgb\b|t\.g\.b|innopark|depark|ulutek|\betgb\b"
)
_TEKNOPARK_NAMES: dict[str, str] = {
    "bilisim vadisi": "Bilişim Vadisi", "cyberpark": "Bilkent Cyberpark", "teknopark istanbul": "Teknopark İstanbul",
    "odtu teknokent": "ODTÜ Teknokent", "hacettepe teknokent": "Hacettepe Teknokent", "gazi teknopark": "Gazi Teknopark",
    "ankara teknopark": "Ankara Teknopark", "antalya teknokent": "Antalya Teknokent",
    "etgb": "Eskişehir Teknoloji Geliştirme Bölgesi", "sakarya universitesi teknoloji gelistirme": "Sakarya Teknokent",
    "itu ari": "İTÜ ARI Teknokent", "ari teknokent": "İTÜ ARI Teknokent", "yildiz teknopark": "Yıldız Teknopark",
    "innopark": "Konya Teknokent (İnnopark)", "depark": "DEPARK", "izmir teknoloji": "İzmir Teknoloji Geliştirme Bölgesi",
    "teknopark izmir": "Teknopark İzmir", "ulutek": "ULUTEK", "konya teknokent": "Konya Teknokent",
    "erciyes teknopark": "Erciyes Teknopark", "cukurova teknokent": "Çukurova Teknokent",
}


@dataclass(frozen=True)
class CityGuess:
    """Adres/taksonomi üzerinden şehir tahmini. method: lokasyon | province | district | None."""

    city: str | None = None
    district: str | None = None
    method: str | None = None


def _fold_for_match(text: str) -> str:
    t = ascii_fold(text)
    t = re.sub(r"[/,;:()\[\]\-–—|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _int_list(v: Any) -> list[int]:
    """WordPress terim id listesini tolere ederek int listesine çevirir (liste değilse de çökmez)."""
    if v is None or isinstance(v, (str, bytes, dict)):
        return []
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = [v]
    out: list[int] = []
    try:
        for x in v:
            if isinstance(x, bool) or x is None:
                continue
            try:
                out.append(int(str(x).strip()))
            except (TypeError, ValueError):
                continue
    except TypeError:
        return []
    return out


def _province_from_term(name: str | None) -> str | None:
    """Taksonomi terim adını il adına çevirir; il değilse ("Yurt Dışı" vb.) None."""
    if not name:
        return None
    key = _fold_for_match(str(name))
    if key in _PROVINCE_BY_KEY:
        return normalize_city(_PROVINCE_BY_KEY[key])
    disp = _DISTRICTS.get(key)
    if disp:
        return normalize_city(disp[1])
    guess = city_from_text(name)
    return guess.city


def city_from_text(text: str | None) -> CityGuess:
    """Adres metninden il (ve mümkünse ilçe) çıkarır.

    Sıra: (1) il adı — sondaki eşleşme alınır, çünkü adresler şehirle biter
    ("... Çankaya/Ankara İstanbul Ofis: ... Kadıköy/İstanbul" -> İstanbul değil;
    yalnızca tek ofisli adreslerde güvenilir). "Ankara Caddesi" gibi sokak adları sayılmaz.
    (2) ilçe/teknokent haritası. (3) hiçbiri yoksa city=None.
    """
    if not text:
        return CityGuess()
    folded = _fold_for_match(str(text))
    if not folded:
        return CityGuess()
    prov_matches = list(_PROVINCE_RE.finditer(folded))
    dist_matches = list(_DISTRICT_RE.finditer(folded))
    if prov_matches:
        city = _PROVINCE_BY_KEY[prov_matches[-1].group(1)]
        district = None
        for m in dist_matches:
            disp, dcity = _DISTRICTS[m.group(1)]
            if dcity == city:
                district = disp
        return CityGuess(city=normalize_city(city), district=district, method="province")
    if dist_matches:
        disp, dcity = _DISTRICTS[dist_matches[-1].group(1)]
        return CityGuess(city=normalize_city(dcity), district=disp, method="district")
    return CityGuess()


def resolve_city(
    lokasyon_ids: Iterable[int] | None,
    address: str | None,
    lokasyon_map: dict[int, str] | None = None,
) -> CityGuess:
    """YASAD kaydı için şehir: önce lokasyon terimi, sonra adres metni."""
    lokasyon_map = lokasyon_map or YASAD_LOKASYON_FALLBACK
    guess_from_address = city_from_text(address)
    for tid in _int_list(lokasyon_ids):
        name = lokasyon_map.get(tid)
        city = _province_from_term(name) if name else None
        if city:
            district = guess_from_address.district if guess_from_address.city == city else None
            if district is None:
                # ilçe adını adresten şehre bakmadan dene
                folded = _fold_for_match(address or "")
                for m in _DISTRICT_RE.finditer(folded):
                    disp, dcity = _DISTRICTS[m.group(1)]
                    if dcity == city:
                        district = disp
            return CityGuess(city=city, district=district, method="lokasyon")
    return guess_from_address


def teknopark_hint(address: str | None) -> tuple[bool, str | None]:
    """Adres teknokent/teknopark içinde mi? (bayrak, bilinen teknopark adı ya da None)."""
    if not address:
        return False, None
    folded = _fold_for_match(address)
    if not _TEKNOPARK_RE.search(folded):
        return False, None
    for key in sorted(_TEKNOPARK_NAMES, key=len, reverse=True):
        if key in folded:
            return True, _TEKNOPARK_NAMES[key]
    return True, None


# --- URL / metin yardımcıları ----------------------------------------------------

_SCHEME_RE = re.compile(r"^(?:https?://)+", re.I)
# mailto:, javascript:, tel:, ftp: gibi http dışı şemalar (":" ardından port rakamı gelmiyorsa)
_OTHER_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:(?!\d)", re.I)


def normalize_website(raw: Any) -> str | None:
    """Web adresini düzeltir: boşluk, "http://https://x" hatası, şema eksikliği, büyük harfli alan adı.

    http(s) dışı şemalar (mailto:, javascript:, tel:) ve alan adı olmayan değerler için None.
    """
    if raw is None or not isinstance(raw, str):
        return None
    u = raw.strip().strip("\"'").strip()
    if not u or " " in u:
        return None
    if u.lower() in ("http://", "https://", "http", "https", "www", "www.", "-", "yok", "n/a"):
        return None
    # "http://https://site.com" -> "https://site.com" (son şema korunur)
    m = _SCHEME_RE.match(u)
    if m:
        schemes = re.findall(r"https?://", m.group(0), re.I)
        u = schemes[-1].lower() + u[m.end():]
    elif _OTHER_SCHEME_RE.match(u) or u.startswith("//"):
        return None
    else:
        u = "https://" + u
    try:
        p = urlparse(u)
    except ValueError:
        return None
    # Türkçe "İ".lower() birleşik nokta (U+0307) üretir; alan adında düz "i" olmalı.
    host = (p.hostname or "").strip(".").replace("İ", "i").lower()
    if not host or "." not in host:
        return None
    netloc = host + (f":{p.port}" if p.port else "")
    u = p._replace(netloc=netloc).geturl()
    if domain_of(u) is None:
        return None
    return u


def site_origin(url: str | None) -> str | None:
    """Derin sayfa bağlantısını kök adrese indirger: https://x.com/sayfa/hakkinda -> https://x.com"""
    u = normalize_website(url)
    if not u:
        return None
    p = urlparse(u)
    if not p.hostname:
        return None
    return f"{p.scheme}://{p.hostname}"


def _is_linkedin(url: str | None) -> bool:
    d = domain_of(url) if url else None
    return bool(d) and d.endswith("linkedin.com")


def _text(v: Any, limit: int | None = None) -> str | None:
    if v is None or v is False:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    return clean_text(html.unescape(v), limit)


def _phone(v: Any) -> str | None:
    s = _text(v)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits or len(digits) < 7:
        return None
    if s.isdigit() and len(digits) == 10:
        return "0" + digits
    if digits.startswith("90") and len(digits) == 12:
        return "0" + digits[2:]
    return s


def _int_or_none(v: Any) -> int | None:
    """Kuruluş yılı gibi 1800-2100 arası tam sayı; "1984", 1984.0 kabul, gerisi None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(float(str(v).strip().replace(",", ".")))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if 1800 <= n <= 2100 else None


def _company_emails(v: Any) -> list[str]:
    """Alan birden çok adres içerebilir ("info@x.com; satis@x.com"); geçerli olanları küçük harfle döner."""
    s = _text(v)
    if not s:
        return []
    out: list[str] = []
    for part in re.split(r"[;,/\s|]+", s.lower().replace("mailto:", "")):
        e = part.strip().strip("<>()[]\"'")
        if e and is_valid_email(e) and e not in out:
            out.append(e)
    return out


def _company_email(v: Any) -> str | None:
    emails = _company_emails(v)
    return emails[0] if emails else None


_MEMBER_CLASS_RE = re.compile(r"^\s*([a-dA-D])(?![a-zA-Z0-9])")


def _member_class(v: Any) -> str | None:
    """"A", " b ", "A Sınıfı" -> "A"; tanınmazsa None."""
    s = _text(v)
    if not s:
        return None
    m = _MEMBER_CLASS_RE.match(s)
    return m.group(1).upper() if m else None


def _is_generic_mailbox(email: str) -> bool:
    """Kişisel değil, kurumsal görünen posta kutusu mu? (ad.soyad@ gibi olanlar atlanır)"""
    local = email.split("@", 1)[0]
    generic = (
        "info", "iletisim", "contact", "bilgi", "kariyer", "career", "ik", "hr", "insankaynaklari",
        "staj", "intern", "basvuru", "jobs", "job", "recruit", "destek", "support", "sales", "satis",
        "hello", "merhaba", "office", "ofis", "admin", "mail", "yasad",
    )
    return any(local == g or local.startswith(g) for g in generic)


def term_map(terms: Iterable[dict] | dict[int, str] | None) -> dict[int, str]:
    """WordPress taksonomi terim listesini {id: ad} haritasına çevirir."""
    if not terms or isinstance(terms, (str, bytes)):
        return {}
    out: dict[int, str] = {}
    items: Iterable[tuple[Any, Any]]
    if isinstance(terms, dict):
        items = terms.items()
    else:
        try:
            items = ((t.get("id"), t.get("name")) for t in terms if isinstance(t, dict))
        except TypeError:
            return {}
    for tid, name in items:
        if tid is None or name is None or isinstance(tid, bool):
            continue
        try:
            key = int(float(str(tid).strip()))
        except (TypeError, ValueError, OverflowError):
            continue
        label = clean_text(html.unescape(str(name)))
        if label:
            out[key] = label
    return out


def filter_by_cities(raws: Iterable[RawCompany], cities: list[str] | None, keep_unknown: bool = True) -> Iterator[RawCompany]:
    """Şehri bilinen kayıtları hedef listeye göre süzer; şehri bilinmeyenleri (varsayılan) geçirir."""
    if not cities:
        yield from raws
        return
    targets = [normalize_city(c) or c for c in cities]
    for r in raws:
        if r.city is None:
            if keep_unknown:
                yield r
            continue
        if is_target_city(normalize_city(r.city), targets):
            yield r


# --- YASAD: REST ayrıştırma ------------------------------------------------------

def _rest_name(rec: dict, acf: dict) -> tuple[str | None, str | None]:
    """(görünen ad, hukuki unvan). Görünen ad kart başlığı (title), yoksa Sirket_Unvani."""
    title = rec.get("title")
    if isinstance(title, dict):
        title = title.get("rendered")
    display = _text(title, 255)
    legal = _text(acf.get("Sirket_Unvani"), 255)
    if not display:
        display = legal
    return display, legal


def parse_yasad_rest_record(
    rec: dict,
    *,
    lokasyon: dict[int, str] | None = None,
    sektor: dict[int, str] | None = None,
) -> RawCompany | None:
    """Tek bir WordPress `firmalar` kaydını RawCompany'ye çevirir. Adı yoksa None."""
    if not isinstance(rec, dict):
        return None
    acf = rec.get("acf") if isinstance(rec.get("acf"), dict) else {}
    name, legal = _rest_name(rec, acf)
    if not name:
        return None
    lokasyon = lokasyon or YASAD_LOKASYON_FALLBACK
    sektor = sektor or YASAD_SEKTOR_FALLBACK

    address = _text(acf.get("Sirket_Adres"), 500)
    lok_ids = _int_list(rec.get("lokasyon"))
    guess = resolve_city(lok_ids, address, lokasyon)

    cat_ids = _int_list(rec.get("sektor-kategorisi"))
    categories: list[str] = []
    for i in cat_ids:
        if i in sektor and sektor[i] not in categories:
            categories.append(sektor[i])
    durum_ids = set(_int_list(rec.get("firma-durum")))
    is_member = bool(durum_ids & YASAD_TERM_MEMBER)
    is_catalog = bool(durum_ids & YASAD_TERM_CATALOG) or acf.get("katalog_firmasi_mi") is True
    member_class = _member_class(acf.get("uyelik_sinifi"))

    emails: list[str] = []
    for e in _company_emails(acf.get("e-mail_adresi")):
        if e not in emails:
            emails.append(e)
    # Yetkili e-postası yalnızca kurumsal (info@ vb.) görünüyorsa alınır; kişisel veri saklanmaz.
    for e in _company_emails(acf.get("Sirket_Yetkilisi_E-Posta")):
        if e not in emails and _is_generic_mailbox(e):
            emails.append(e)

    linkedin = _text(acf.get("Sirket_Linkedin_Hesabi"))
    if linkedin and not _is_linkedin(linkedin):
        linkedin = None

    is_tp, tp_name = teknopark_hint(address)
    tags = [SOURCE_YASAD]
    if is_member:
        tags.append("yasad-uye")
    if is_catalog:
        tags.append("yasad-katalog")
    if member_class:
        tags.append(f"yasad-sinif-{member_class.lower()}")
    for c in categories:
        if c not in tags:
            tags.append(c)
    if guess.city is None:
        tags.append(UNKNOWN_CITY_TAG)

    slug = _text(rec.get("slug"))
    source_url = normalize_website(_text(rec.get("link"))) or (f"{YASAD_BASE}/firmalar/{slug}/" if slug else YASAD_LIST_URL)
    raw = {
        "id": rec.get("id"),
        "slug": rec.get("slug"),
        "legal_name": legal,
        "title": name,
        "member_class": member_class,
        "is_member": is_member,
        "is_catalog": is_catalog,
        "categories": categories,
        "category_ids": cat_ids,
        "lokasyon_ids": lok_ids,
        "founded_year": _int_or_none(acf.get("kurulus_yili")),
        "business_type": _text(acf.get("isletme_tipi")),
        "city_method": guess.method,
        "city_unknown": guess.city is None,
        "detail_url": source_url,
        "via": "rest",
    }
    return RawCompany(
        name=name,
        source=SOURCE_YASAD,
        source_url=source_url,
        website=normalize_website(acf.get("web_sitesi_adresi")),
        city=guess.city,
        district=guess.district,
        address=address,
        phone=_phone(acf.get("telefon")),
        sector=categories[0] if categories else None,
        description=_text(acf.get("sirket_hakkinda"), 1000),
        tags=tags,
        emails=emails,
        linkedin_url=linkedin,
        is_teknopark=is_tp,
        teknopark_name=tp_name,
        raw=raw,
    )


def parse_yasad_rest(
    records: Iterable[dict],
    *,
    lokasyon: Iterable[dict] | dict[int, str] | None = None,
    sektor: Iterable[dict] | dict[int, str] | None = None,
) -> list[RawCompany]:
    """REST kayıt listesini (bir sayfa ya da hepsi) RawCompany listesine çevirir. Çevrimdışı çalışır."""
    lok_map = term_map(lokasyon) or YASAD_LOKASYON_FALLBACK
    sek_map = term_map(sektor) or YASAD_SEKTOR_FALLBACK
    out: list[RawCompany] = []
    skipped = broken = 0
    if isinstance(records, (str, bytes, dict)):
        records = []
    for rec in records or []:
        try:
            rc = parse_yasad_rest_record(rec, lokasyon=lok_map, sektor=sek_map)
        except Exception as e:  # tek bozuk kayıt sayfanın tamamını düşürmesin
            broken += 1
            log.warning("YASAD kaydı ayrıştırılamadı (id=%s): %s", rec.get("id") if isinstance(rec, dict) else "?", e)
            continue
        if rc is None:
            skipped += 1
            continue
        out.append(rc)
    if skipped:
        log.info("YASAD: adı olmayan %d kayıt atlandı", skipped)
    if broken:
        log.warning("YASAD: bozuk %d kayıt atlandı", broken)
    return out


# --- YASAD: HTML yedek ayrıştırma ------------------------------------------------

@dataclass
class YasadCard:
    name: str
    detail_url: str | None
    logo_alt: str | None = None


def parse_yasad_listing(html_text: str, base_url: str = YASAD_LIST_URL) -> tuple[list[YasadCard], str | None]:
    """/uyelerimiz/ liste sayfası: (kartlar, sonraki sayfa URL'si)."""
    tree = LexborHTMLParser(html_text)
    cards: list[YasadCard] = []
    nodes = tree.css("ul.fusion-grid-posts-cards > li.post-card") or tree.css("li.post-card")
    for li in nodes:
        h = li.css_first("h2.fusion-title-heading") or li.css_first("h2, h3")
        name = clean_text(h.text(), 255) if h else None
        a = li.css_first('a.fusion-button[href*="/firmalar/"]') or li.css_first('a[href*="/firmalar/"]')
        href = urljoin(base_url, a.attributes.get("href", "")) if a else None
        if not name and href:
            name = href.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        if not name:
            continue
        img = li.css_first("img")
        alt = clean_text(img.attributes.get("alt")) if img else None
        cards.append(YasadCard(name=name, detail_url=href, logo_alt=alt))
    nxt = tree.css_first("div.pagination a.pagination-next") or tree.css_first('a[rel="next"]')
    next_url = urljoin(base_url, nxt.attributes.get("href", "")) if nxt and nxt.attributes.get("href") else None
    return cards, next_url


_DETAIL_CLASS_RE = re.compile(r"\b([A-D])\s*Sınıfı", re.I)
_DETAIL_YEAR_RE = re.compile(r"Kuruluş\s*(?:Yılı)?\s*:?\s*(\d{4})\b")
_DETAIL_ADDRESS_RE = re.compile(r"Adres\s*:\s*(.+)")


def parse_yasad_detail(html_text: str, url: str | None = None) -> dict[str, Any]:
    """/firmalar/<slug>/ detay sayfası: name, website, phone, address, member_class, founded_year."""
    tree = LexborHTMLParser(html_text)
    out: dict[str, Any] = {
        "name": None, "website": None, "phone": None, "address": None,
        "member_class": None, "founded_year": None, "emails": [],
    }
    for a in tree.css("a[href]"):
        href = a.attributes.get("href", "").strip()
        txt = clean_text(a.text()) or ""
        if href.startswith("tel:") and not out["phone"]:
            out["phone"] = _phone(href[4:])
        elif href.startswith("mailto:"):
            e = _company_email(href[7:].split("?")[0])
            if e and e not in out["emails"]:
                out["emails"].append(e)
        elif "web sitemizi" in txt.lower() and href.startswith("http") and not out["website"]:
            out["website"] = normalize_website(href)
    body = tree.body.text(separator="\n", strip=True) if tree.body else tree.root.text(separator="\n", strip=True)
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if out["name"] is None and line.endswith("Hakkında") and len(line) < 150:
            out["name"] = clean_text(line[: -len("Hakkında")])
        m = _DETAIL_ADDRESS_RE.match(line)
        if m and not out["address"]:
            out["address"] = clean_text(m.group(1), 500)
    m = _DETAIL_CLASS_RE.search(body)
    if m:
        out["member_class"] = m.group(1).upper()
    m = _DETAIL_YEAR_RE.search(body)
    if m:
        out["founded_year"] = _int_or_none(m.group(1))
    if not out["website"]:
        # Yedek: yasad.org.tr dışına giden ilk http bağlantısı
        for a in tree.css("a[href^='http']"):
            href = a.attributes.get("href", "")
            d = domain_of(href)
            if d and not d.endswith("yasad.org.tr") and not _is_linkedin(href) and "wp-content" not in href:
                out["website"] = normalize_website(href)
                break
    if not out["name"] and url:
        out["name"] = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
    return out


def yasad_from_html(card: YasadCard, detail: dict[str, Any] | None = None) -> RawCompany:
    """Liste kartı + detay sayfası verisinden RawCompany üretir."""
    detail = detail or {}
    address = detail.get("address")
    guess = city_from_text(address)
    is_tp, tp_name = teknopark_hint(address)
    member_class = detail.get("member_class")
    tags = [SOURCE_YASAD, "yasad-uye"]
    if member_class:
        tags.append(f"yasad-sinif-{member_class.lower()}")
    if guess.city is None:
        tags.append(UNKNOWN_CITY_TAG)
    name = clean_text(card.name or detail.get("name"), 255) or "?"
    return RawCompany(
        name=name,
        source=SOURCE_YASAD,
        source_url=card.detail_url or YASAD_LIST_URL,
        website=detail.get("website"),
        city=guess.city,
        district=guess.district,
        address=address,
        phone=detail.get("phone"),
        tags=tags,
        emails=list(detail.get("emails") or []),
        is_teknopark=is_tp,
        teknopark_name=tp_name,
        raw={
            "legal_name": detail.get("name") if detail.get("name") != name else None,
            "member_class": member_class,
            "is_member": True,
            "founded_year": detail.get("founded_year"),
            "logo_alt": card.logo_alt,
            "city_method": guess.method,
            "city_unknown": guess.city is None,
            "detail_url": card.detail_url,
            "via": "html",
        },
    )


# --- TÜBİSAD ---------------------------------------------------------------------

_LOGO_SLUG_RE = re.compile(r"logo-([^./?]+)")


def parse_tubisad(html_text: str, source_url: str = TUBISAD_URL) -> list[RawCompany]:
    """Kurumsal üye tablosundaki bağlantıları RawCompany listesine çevirir (şehir yok)."""
    tree = LexborHTMLParser(html_text)
    anchors = tree.css("div.HTMLContent table td a[href]") or tree.css("div.HTMLContent a[href]") or tree.css("table td a[href]")
    out: list[RawCompany] = []
    seen: set[str] = set()
    for a in anchors:
        href = (a.attributes.get("href") or "").strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        span = a.css_first("span")
        img = a.css_first("img")
        name: str | None = None
        name_source = None
        if span and clean_text(span.text()):
            name = clean_text(span.text(), 255)
            name_source = "span"
        if not name:
            alt = clean_text(img.attributes.get("alt")) if img else None
            if alt and len(alt) > 1:
                name, name_source = alt, "alt"
        slug = None
        if img:
            m = _LOGO_SLUG_RE.search(img.attributes.get("src") or "")
            slug = m.group(1) if m else None
        if not name and slug:
            name, name_source = slug[:1].upper() + slug[1:], "logo-slug"
        if not name:
            txt = clean_text(a.text(), 255)
            if txt:
                name, name_source = txt, "text"
        website: str | None
        linkedin: str | None = None
        if _is_linkedin(href):
            website, linkedin = None, href
            key = href.lower().rstrip("/")
        else:
            website = site_origin(href)
            if website is None:
                continue
            key = domain_of(website) or website
            if not name:
                label = key.split(".")[0]
                name, name_source = label[:1].upper() + label[1:], "domain"
        if not name or key in seen:
            continue
        seen.add(key)
        tags = [SOURCE_TUBISAD, "tubisad-uye", UNKNOWN_CITY_TAG]
        if name_source in ("logo-slug", "domain"):
            tags.append(GUESSED_NAME_TAG)
        out.append(
            RawCompany(
                name=name,
                source=SOURCE_TUBISAD,
                source_url=source_url,
                website=website,
                city=None,
                tags=tags,
                linkedin_url=linkedin,
                raw={
                    "slug": slug,
                    "name_source": name_source,
                    "original_href": href,
                    "city_unknown": True,
                },
            )
        )
    return out


# --- Bilişim 500 (elle dışa aktarılan CSV) ---------------------------------------

_B500_COLS = {
    "rank": ("sira", "sıra", "rank", "no", "siralama"),
    "name": ("sirket", "şirket", "firma", "company", "name", "ad", "unvan", "kurum"),
    "category": ("kategori", "category", "sektor", "sektör", "alan"),
    "revenue": ("ciro", "revenue", "gelir", "satis", "satış"),
    "city": ("sehir", "şehir", "il", "city", "lokasyon"),
    "website": ("web", "website", "site", "url", "domain"),
    "year": ("yil", "yıl", "year"),
}


def _b500_col(headers: list[str], key: str) -> int | None:
    folded = [ascii_fold(h) for h in headers]
    for cand in _B500_COLS[key]:
        c = ascii_fold(cand)
        for i, h in enumerate(folded):
            if h == c or h.startswith(c + " ") or h.startswith(c + "_"):
                return i
    return None


def _read_csv_text(p: Path) -> str:
    """CSV metnini okur: UTF-8 (BOM'lu/BOM'suz), olmazsa Türkçe Excel'in cp1254 kodlaması."""
    data = p.read_bytes()
    for enc in ("utf-8-sig", "cp1254"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_bilisim500_csv(path: Path | str, year: int | None = None) -> list[RawCompany]:
    """``Sıra;Şirket;Kategori;Ciro[;Şehir;Web]`` sütunlu CSV'yi okur (noktalı virgül ya da virgül)."""
    p = Path(path)
    text = _read_csv_text(p)
    if year is None:
        m = re.search(r"(20\d{2})", p.stem)
        year = int(m.group(1)) if m else None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    # Ayırıcı: başlık satırında en çok geçen (Sniffer düzensiz satırlarda yanılıyor)
    header_line = lines[0]
    delimiter = max(";,\t", key=header_line.count) if any(d in header_line for d in ";,\t") else ";"
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    headers = [h.strip() for h in rows[0]]
    idx = {k: _b500_col(headers, k) for k in _B500_COLS}
    if idx["name"] is None:
        log.warning("Bilişim 500 CSV'sinde şirket sütunu bulunamadı: %s", p)
        return []
    try:
        source_url = p.resolve().as_uri()  # Windows: file:///C:/... biçimi
    except (OSError, ValueError):
        source_url = f"file:///{p.as_posix().lstrip('/')}"
    out: list[RawCompany] = []
    for row in rows[1:]:
        def cell(k: str) -> str | None:
            i = idx.get(k)
            return clean_text(row[i]) if i is not None and i < len(row) else None

        name = cell("name")
        if not name:
            continue
        rank = None
        r = cell("rank")
        if r and re.sub(r"\D", "", r):
            rank = int(re.sub(r"\D", "", r))
        row_year = _int_or_none(cell("year")) or year
        city_raw = cell("city")
        guess = city_from_text(city_raw) if city_raw else CityGuess()
        city = guess.city or (normalize_city(city_raw) if city_raw else None)
        category = cell("category")
        tags = [SOURCE_BILISIM500]
        if row_year:
            tags.append(f"bilisim500-{row_year}")
        if category:
            tags.append(category)
        if city is None:
            tags.append(UNKNOWN_CITY_TAG)
        out.append(
            RawCompany(
                name=clean_text(name, 255),
                source=SOURCE_BILISIM500,
                source_url=source_url,
                website=normalize_website(cell("website")),
                city=city,
                sector=category,
                tags=tags,
                raw={"rank": rank, "category": category, "revenue": cell("revenue"), "year": row_year, "city_unknown": city is None},
            )
        )
    return out


# --- Collector sınıfları -----------------------------------------------------------

class _HttpCollector:
    """Ortak HTTP altyapısı: istemci, bekleme, robots.txt denetimi, hatada boş dönüş."""

    name: str = ""

    def __init__(self, client: httpx.Client | None = None, throttle: Throttle | None = None, *, check_robots: bool = True):
        self._client = client
        self._owns_client = client is None
        self.throttle = throttle or Throttle()
        self.check_robots = check_robots
        self._robots: dict[str, RobotFileParser] = {}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_http_client()
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None

    def _allowed(self, url: str) -> bool:
        if not self.check_robots:
            return True
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        rp = self._robots.get(host)
        if rp is None:
            rp = RobotFileParser()
            try:
                r = self.client.get(host + "/robots.txt")
                if r.status_code == 200 and r.text:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True
            except Exception:
                rp.allow_all = True
            self._robots[host] = rp
        try:
            return rp.can_fetch(get_settings().http_user_agent, url)
        except Exception:  # pragma: no cover
            return True

    def get(self, url: str, **kw) -> httpx.Response | None:
        if not self._allowed(url):
            log.warning("robots.txt izin vermiyor, atlanıyor: %s", url)
            return None
        return safe_get(self.client, url, self.throttle, **kw)

    def get_json(self, url: str, **kw) -> tuple[Any, httpx.Response | None]:
        r = self.get(url, **kw)
        if r is None:
            return None, None
        try:
            return r.json(), r
        except ValueError:
            log.warning("JSON ayrıştırılamadı: %s", url)
            return None, r

    def collect(self, cities: list[str] | None = None) -> Iterator[RawCompany]:
        """Hata olursa loglar ve akışı sonlandırır; exception fırlatmaz."""
        n = 0
        try:
            for rc in filter_by_cities(self._collect(), cities):
                n += 1
                yield rc
        except Exception:
            log.exception("%s kaynağı toplanırken beklenmeyen hata; kaynak atlanıyor", self.name)
        finally:
            self.close()
            log.info("%s: %d şirket üretildi", self.name, n)

    def _collect(self) -> Iterator[RawCompany]:  # pragma: no cover - alt sınıflar uygular
        return iter(())


class YasadCollector(_HttpCollector):
    """YASAD üye/katalog firmaları: WordPress REST (birincil) + HTML liste/detay (yedek)."""

    name = SOURCE_YASAD

    def __init__(
        self,
        client: httpx.Client | None = None,
        throttle: Throttle | None = None,
        *,
        use_rest: bool = True,
        html_fallback: bool = True,
        max_pages: int | None = None,
        check_robots: bool = True,
    ):
        super().__init__(client, throttle, check_robots=check_robots)
        self.use_rest = use_rest
        self.html_fallback = html_fallback
        self.max_pages = max_pages

    # REST ---------------------------------------------------------------
    def fetch_taxonomy(self, tax: str) -> dict[int, str]:
        data, _ = self.get_json(YASAD_TAX_URL.format(tax=tax), params={"per_page": 100})
        if not isinstance(data, list):
            log.warning("YASAD taksonomisi alınamadı, yedek harita kullanılıyor: %s", tax)
            return {}
        return term_map(data)

    def _collect_rest(self) -> list[RawCompany] | None:
        """Tüm sayfaları çeker. REST kullanılamıyorsa None döner (yedek yola geçilir)."""
        lok = self.fetch_taxonomy("lokasyon") or YASAD_LOKASYON_FALLBACK
        sek = self.fetch_taxonomy("sektor-kategorisi") or YASAD_SEKTOR_FALLBACK
        out: list[RawCompany] = []
        page, total_pages = 1, 1
        limit = self.max_pages or YASAD_MAX_REST_PAGES
        while page <= total_pages and page <= limit:
            params = {"per_page": YASAD_PER_PAGE, "page": page, "_fields": YASAD_REST_FIELDS}
            data, resp = self.get_json(YASAD_REST_URL, params=params)
            if resp is None or not isinstance(data, list):
                if page == 1:
                    log.warning("YASAD REST API yanıt vermedi")
                    return None
                break  # 400 rest_post_invalid_page_number ya da ağ hatası: eldekiyle devam
            if page == 1:
                if not data:
                    log.warning("YASAD REST API boş liste döndürdü")
                    return None
                if not any(isinstance(r, dict) and isinstance(r.get("acf"), dict) for r in data):
                    log.warning("YASAD REST kayıtlarında 'acf' alanı yok (ACF REST kapatılmış olabilir)")
                    return None
                try:
                    total_pages = max(1, int(resp.headers.get("X-WP-TotalPages", "1")))
                except ValueError:
                    total_pages = 1
                log.info("YASAD REST: %s kayıt, %d sayfa", resp.headers.get("X-WP-Total", "?"), total_pages)
            if not data:
                break
            out.extend(parse_yasad_rest(data, lokasyon=lok, sektor=sek))
            page += 1
        return out

    # HTML -----------------------------------------------------------------
    def _collect_html(self) -> Iterator[RawCompany]:
        url: str | None = YASAD_LIST_URL
        pages = 0
        seen: set[str] = set()
        visited_pages: set[str] = set()
        limit = self.max_pages or YASAD_MAX_HTML_PAGES
        while url and pages < limit:
            if url in visited_pages:
                break  # sayfalama döngüsü (page/2 -> page/1 gibi)
            visited_pages.add(url)
            pages += 1
            r = self.get(url)
            if r is None:
                log.warning("YASAD liste sayfası alınamadı: %s", url)
                break
            cards, next_url = parse_yasad_listing(r.text, url)
            log.info("YASAD HTML sayfa %d: %d firma", pages, len(cards))
            for card in cards:
                if card.detail_url and card.detail_url in seen:
                    continue
                if card.detail_url:
                    seen.add(card.detail_url)
                detail: dict[str, Any] = {}
                if card.detail_url:
                    dr = self.get(card.detail_url)
                    if dr is not None:
                        detail = parse_yasad_detail(dr.text, card.detail_url)
                yield yasad_from_html(card, detail)
            if next_url == url:
                break
            url = next_url

    def _collect(self) -> Iterator[RawCompany]:
        if self.use_rest:
            result = self._collect_rest()
            if result is not None:
                yield from result
                return
            if not self.html_fallback:
                return
            log.warning("YASAD: REST başarısız, HTML liste sayfalarına geçiliyor")
        yield from self._collect_html()


class TubisadCollector(_HttpCollector):
    """TÜBİSAD kurumsal üye listesi (yalnızca web sitesi; şehir bilinmez)."""

    name = SOURCE_TUBISAD

    def __init__(self, client: httpx.Client | None = None, throttle: Throttle | None = None, *, url: str = TUBISAD_URL, check_robots: bool = True):
        super().__init__(client, throttle, check_robots=check_robots)
        self.url = url

    def _collect(self) -> Iterator[RawCompany]:
        r = self.get(self.url)
        if r is None:
            log.warning("TÜBİSAD üye listesi alınamadı: %s", self.url)
            return
        items = parse_tubisad(r.text, self.url)
        if not items:
            log.warning("TÜBİSAD sayfasında üye bağlantısı bulunamadı (sayfa yapısı değişmiş olabilir)")
        yield from items


class Bilisim500Collector:
    """Bilişim 500: yalnızca kullanıcı tarafından `data/manual/` altına konan CSV'yi okur.

    Liste ücretli olduğundan ağ taraması yapılmaz. Dosya yoksa bilgilendirir ve boş döner.
    """

    name = SOURCE_BILISIM500

    def __init__(self, manual_dir: Path | None = None):
        self.manual_dir = manual_dir

    def _dir(self) -> Path:
        if self.manual_dir is not None:
            return Path(self.manual_dir)
        return get_settings().abs_data_dir / "manual"

    def collect(self, cities: list[str] | None = None) -> Iterator[RawCompany]:
        try:
            d = self._dir()
            files = sorted(d.glob("bilisim500*.csv")) if d.exists() else []
            if not files:
                log.info(
                    "Bilişim 500 listesi ücretli (store.bthaber.com). Satın alırsanız CSV olarak %s altına "
                    "'bilisim500_<yil>.csv' adıyla koyun; şimdilik atlanıyor.",
                    d,
                )
                return
            for f in files:
                try:
                    items = parse_bilisim500_csv(f)
                except (OSError, UnicodeDecodeError, csv.Error) as e:
                    log.warning("Bilişim 500 CSV okunamadı %s: %s", f, e)
                    continue
                log.info("Bilişim 500 CSV %s: %d şirket", f.name, len(items))
                yield from filter_by_cities(items, cities)
        except Exception:
            log.exception("Bilişim 500 kaynağı toplanırken beklenmeyen hata; kaynak atlanıyor")


SOURCES: list[type] = [YasadCollector, TubisadCollector, Bilisim500Collector]
SOURCE_NAMES: list[str] = [c.name for c in SOURCES]


def get_collector(name: str, **kwargs):
    """Ada göre collector örneği ('yasad' | 'tubisad' | 'bilisim500'). Tanınmazsa ValueError."""
    for cls in SOURCES:
        if cls.name == name:
            return cls(**kwargs)
    raise ValueError(f"Bilinmeyen kaynak: {name} (geçerli: {', '.join(SOURCE_NAMES)})")


class _hybridmethod:
    """Hem sınıf hem örnek üzerinden çağrılabilen metot (ilk argüman: sınıf ya da örnek)."""

    def __init__(self, func):
        self.func = func
        self.__doc__ = func.__doc__

    def __get__(self, obj, cls=None):
        return types.MethodType(self.func, obj if obj is not None else cls)


class AssociationCollectors:
    """Tüm dernek/liste kaynaklarını zincirler. `AssociationCollectors.collect(cities)` doğrudan çağrılabilir."""

    name = SOURCE_BILISIM500

    def __init__(self, collectors: list | None = None, client: httpx.Client | None = None, throttle: Throttle | None = None):
        self.collectors = collectors
        self._client = client
        self._throttle = throttle

    def _instances(self, client: httpx.Client) -> list:
        if self.collectors is not None:
            return list(self.collectors)
        throttle = self._throttle or Throttle()
        return [
            YasadCollector(client, throttle),
            TubisadCollector(client, throttle),
            Bilisim500Collector(),
        ]

    def _iter(self, cities: list[str] | None) -> Iterator[RawCompany]:
        own_client = self._client is None and self.collectors is None
        client = self._client or (make_http_client() if own_client else None)
        try:
            for c in self._instances(client):
                try:
                    yield from c.collect(cities)
                except Exception:
                    log.exception("%s kaynağı başarısız; diğer kaynaklarla devam ediliyor", getattr(c, "name", c))
        finally:
            if own_client and client is not None:
                client.close()

    @_hybridmethod
    def collect(cls_or_self, cities: list[str] | None = None, *, collectors: list | None = None, client: httpx.Client | None = None, throttle: Throttle | None = None) -> Iterator[RawCompany]:
        """Sınıf ya da örnek üzerinden çağrılabilir; kaynakları sırayla gezer."""
        if isinstance(cls_or_self, AssociationCollectors):
            inst = cls_or_self
        else:
            inst = cls_or_self(collectors=collectors, client=client, throttle=throttle)
        yield from inst._iter(cities)


def collect(cities: list[str] | None = None) -> Iterator[RawCompany]:
    """CLI için kısayol: `--source bilisim500` tüm dernek kaynaklarını toplar."""
    yield from AssociationCollectors.collect(cities)
