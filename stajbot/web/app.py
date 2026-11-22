"""FastAPI uygulaması: router kaydı, Jinja2 şablonları, statik dosyalar. Bkz. MIMARI.md bölüm 10."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__, db
from ..config import get_settings
from ..enums import (
    APP_STATUS_LABELS,
    SIZE_LABELS,
    Actor,
    AppStatus,
    ContactKind,
    EventType,
    MailStatus,
    MatchMethod,
    ReviewStatus,
    Sentiment,
    SizeBucket,
)
from ..log import get_logger

log = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

REVIEW_LABELS = {
    ReviewStatus.pending: "Bekliyor",
    ReviewStatus.approved: "Onaylı",
    ReviewStatus.rejected: "Reddedildi",
    ReviewStatus.later: "Sonra",
}
MAIL_LABELS = {
    MailStatus.draft: "Taslak",
    MailStatus.approved: "Onaylı",
    MailStatus.queued: "Kuyrukta",
    MailStatus.sent: "Gönderildi",
    MailStatus.failed: "Hatalı",
    MailStatus.received: "Gelen",
}
SENTIMENT_LABELS = {
    Sentiment.positive: "Olumlu",
    Sentiment.negative: "Olumsuz",
    Sentiment.info: "Bilgi istiyor",
    Sentiment.unknown: "Belirsiz",
}
MATCH_LABELS = {
    MatchMethod.domain: "alan adı",
    MatchMethod.message_id: "Message-ID",
    MatchMethod.tracking_code: "takip kodu",
    MatchMethod.manual: "elle",
    MatchMethod.none: "eşleşmedi",
}
KIND_LABELS = {ContactKind.hr: "İK", ContactKind.personal: "Kişisel", ContactKind.info: "Genel", ContactKind.other: "Diğer"}
EVENT_TYPE_LABELS = {
    EventType.status_change: "durum",
    EventType.email_sent: "mail gitti",
    EventType.email_received: "mail geldi",
    EventType.note: "not",
    EventType.call: "telefon",
    EventType.interview: "mülakat",
    EventType.presentation: "sunum",
    EventType.offer: "teklif",
    EventType.enrich: "zenginleştirme",
    EventType.collect: "toplama",
    EventType.manual: "işlem",
}
ACTOR_LABELS = {Actor.system: "sistem", Actor.user: "kullanıcı"}

# Starlette'in İngilizce varsayılan hata metinleri -> Türkçe
HTTP_ERROR_TEXT = {
    400: "Geçersiz istek",
    404: "Sayfa bulunamadı",
    405: "Bu yöntem desteklenmiyor",
    413: "İstek çok büyük",
    500: "Sunucu hatası",
}
_ENGLISH_DETAILS = {"Not Found", "Method Not Allowed", "Bad Request", "Internal Server Error", "Request Entity Too Large", "Unprocessable Entity"}


def fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if not value:
        return "-"
    try:
        return value.strftime(fmt)
    except (AttributeError, ValueError):
        return str(value)


def fmt_date(value: datetime | None) -> str:
    return fmt_dt(value, "%d.%m.%Y")


def safe_href(url: str | None) -> str:
    """Yalnızca http(s) bağlantılarını href olarak verir; ``javascript:`` gibi şemalar boş döner.

    Jinja autoescape metni korur ama ``href`` içindeki şemayı denetlemez; toplayıcı/elle giriş yoluyla
    gelen adresler buradan geçer.
    """
    if not url:
        return ""
    u = str(url).strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.autoescape = True  # select_autoescape() zaten .html için açık; parça şablonları dahil açıkça sabitlenir
templates.env.filters["dt"] = fmt_dt
templates.env.filters["date"] = fmt_date
templates.env.filters["href"] = safe_href
templates.env.globals.update(
    APP_STATUS_LABELS=APP_STATUS_LABELS,
    SIZE_LABELS=SIZE_LABELS,
    REVIEW_LABELS=REVIEW_LABELS,
    MAIL_LABELS=MAIL_LABELS,
    SENTIMENT_LABELS=SENTIMENT_LABELS,
    MATCH_LABELS=MATCH_LABELS,
    KIND_LABELS=KIND_LABELS,
    EVENT_TYPE_LABELS=EVENT_TYPE_LABELS,
    ACTOR_LABELS=ACTOR_LABELS,
    AppStatus=AppStatus,
    ReviewStatus=ReviewStatus,
    MailStatus=MailStatus,
    Sentiment=Sentiment,
    SizeBucket=SizeBucket,
    version=__version__,
)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    """Şablonu ortak bağlamla (ayarlar, flash mesajı) çizer."""
    ctx.setdefault("flash", request.query_params.get("mesaj"))
    ctx.setdefault("settings", get_settings())
    ctx.setdefault("app_name", get_settings().app_name)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, mesaj: str | None = None) -> RedirectResponse:
    if mesaj:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}mesaj={quote(mesaj)}"
    return RedirectResponse(url, status_code=303)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    db.init_db()
    log.info("Web arayüzü hazır (%s)", get_settings().db_url)
    yield


app = FastAPI(title=get_settings().app_name, version=__version__, lifespan=_lifespan, docs_url=None, redoc_url=None)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _error_response(request: Request, status_code: int, detail: str):
    """Hata sayfası (tam sayfa) ya da HTMX için küçük parça; metin her zaman kaçışlanır."""
    if is_htmx(request):
        return HTMLResponse(
            f"<article class='flash bad' role='alert'>Hata {status_code}: {escape(detail)}</article>",
            status_code=status_code,
        )
    return render(request, "error.html", status_code=status_code, code=status_code, detail=detail)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if not detail or detail in _ENGLISH_DETAILS:
        detail = HTTP_ERROR_TEXT.get(exc.status_code, "Hata")
    return _error_response(request, exc.status_code, detail)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """Form/sorgu doğrulama hatası: JSON 422 yerine Türkçe sayfa.

    Yol parametresi bozuksa (``/sirketler/abc``) kayıt yok demektir -> 404; eksik ya da geçersiz
    form/sorgu alanı -> 400 ve alan adları listelenir.
    """
    errors = exc.errors() or []
    locs = [tuple(e.get("loc") or ()) for e in errors]
    if locs and all(loc and loc[0] == "path" for loc in locs):
        return _error_response(request, 404, "Kayıt bulunamadı")
    fields = sorted({str(loc[-1]) for loc in locs if len(loc) >= 2})
    detail = "Geçersiz ya da eksik alan: " + ", ".join(fields) if fields else "Geçersiz istek"
    log.info("Doğrulama hatası %s %s: %s", request.method, request.url.path, fields)
    return _error_response(request, 400, detail)


from .routes import companies, inbox, mail, review, settings_page, stats  # noqa: E402  (app tanımlandıktan sonra)

app.include_router(stats.router)
app.include_router(companies.router)
app.include_router(review.router)
app.include_router(mail.router)
app.include_router(inbox.router)
app.include_router(settings_page.router)
