"""Komut satırı arayüzü (typer). Bkz. MIMARI.md bölüm 11.

Tüm komutlar ``db.init_db()`` ile başlar, ``db.get_session()`` kullanır ve hata durumunda Türkçe bir
hata satırı yazıp sıfır dışı çıkış kodu verir. Diğer modüller (ingest, enrich, mail, web) tembel
(lazy) olarak içe aktarılır; böylece bir modül eksikken bile CLI yüklenir ve diğer komutlar çalışır.
"""
from __future__ import annotations

import csv
import enum
import functools
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

import typer
from sqlalchemy import func, select

from . import __version__, db, services
from .collectors.base import RawCompany
from .config import get_settings
from .enums import (
    APP_STATUS_LABELS,
    AppStatus,
    Direction,
    MailStatus,
    MatchMethod,
    ReviewStatus,
    normalize_city,
)
from .log import get_logger
from .models import Company, Contact, EmailMessage
from .utils import clean_text, is_valid_email, normalize_url, now_utc

log = get_logger(__name__)

app = typer.Typer(
    name="stajbot",
    help="Staj başvuru otomasyonu: şirket topla, zenginleştir, puanla, mail üret, gönder, cevapları izle.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


# --------------------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------------------

class Source(str, enum.Enum):
    argem = "argem"
    teknopark = "teknopark"
    bilisim500 = "bilisim500"
    google_places = "google_places"
    all = "all"


class CsvSource(str, enum.Enum):
    csv = "csv"
    school = "school"


COLLECT_SOURCES: tuple[str, ...] = ("argem", "teknopark", "bilisim500", "google_places")

# CSV şablonu: başlık satırı (İngilizce anahtarlar; Türkçe eş adlar da kabul edilir)
CSV_TEMPLATE_COLUMNS: tuple[str, ...] = (
    "name", "website", "email", "city", "district", "address", "phone",
    "sector", "description", "contact_name", "tags", "notes",
)
_CSV_ALIASES: dict[str, str] = {
    "name": "name", "ad": "name", "sirket": "name", "sirket adi": "name", "firma": "name", "unvan": "name",
    "website": "website", "web": "website", "site": "website", "web sitesi": "website", "url": "website",
    "email": "email", "e-mail": "email", "e mail": "email", "eposta": "email", "e-posta": "email", "e posta": "email", "mail": "email",
    "city": "city", "sehir": "city", "il": "city",
    "district": "district", "ilce": "district",
    "address": "address", "adres": "address",
    "phone": "phone", "telefon": "phone", "tel": "phone",
    "sector": "sector", "sektor": "sector",
    "description": "description", "aciklama": "description",
    "contact_name": "contact_name", "kisi": "contact_name", "yetkili": "contact_name", "ilgili": "contact_name",
    "tags": "tags", "etiketler": "tags", "etiket": "tags",
    "notes": "notes", "not": "notes", "notlar": "notes",
}


def _err(msg: str) -> None:
    typer.echo(f"HATA: {msg}", err=True)


def _warn(msg: str) -> None:
    typer.echo(f"UYARI: {msg}", err=True)


def _load(module: str, hint: str | None = None) -> Any:
    """Modülü tembel içe aktarır; yoksa anlaşılır Türkçe hata verir."""
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name and (e.name == module or module.startswith(e.name)):
            raise RuntimeError(
                f"'{module}' modülü bulunamadı; bu komut için gerekli bileşen henüz kurulu değil."
                + (f" {hint}" if hint else "")
            ) from e
        raise


def _cli_command(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Veritabanını hazırlar, beklenmeyen hatayı Türkçe bildirir ve sıfır dışı çıkış kodu verir."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            db.init_db()
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except KeyboardInterrupt:
            _err("İşlem kullanıcı tarafından durduruldu.")
            raise typer.Exit(code=130)
        except Exception as e:  # noqa: BLE001 - CLI sınırı: her hatayı Türkçe raporla
            log.debug("Komut başarısız: %s", fn.__name__, exc_info=True)
            _err(f"{type(e).__name__}: {e}")
            raise typer.Exit(code=1)

    return wrapper


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Rapor/dataclass/dict üzerinden alan okur (kimi modüller dict döndürebilir)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _cities_or_default(cities: list[str] | None) -> list[str]:
    if cities:
        return [normalize_city(c) or c for c in cities if c and c.strip()]
    return list(get_settings().target_cities)


def _fmt_ingest_report(source: str, report: Any) -> str:
    created = _get(report, "created", 0)
    updated = _get(report, "updated", 0)
    skipped_city = _get(report, "skipped_city", 0)
    skipped_invalid = _get(report, "skipped_invalid", 0)
    errors = list(_get(report, "errors", []) or [])
    line = (
        f"[{source}] yeni: {created}, güncellenen: {updated}, "
        f"hedef dışı şehir: {skipped_city}, geçersiz: {skipped_invalid}, hata: {len(errors)}"
    )
    if errors:
        line += "\n" + "\n".join(f"    - {e}" for e in errors[:10])
        if len(errors) > 10:
            line += f"\n    ... ve {len(errors) - 10} hata daha (ayrıntı için log)"
    return line


def _collector_for(source: str, cities: list[str]) -> Iterator[RawCompany]:
    """Kaynak adına göre collector çalıştırır (ağa çıkar, veritabanına yazmaz)."""
    if source == "argem":
        mod = _load("stajbot.collectors.argem")
        return mod.collect(cities)
    if source == "teknopark":
        mod = _load("stajbot.collectors.teknopark")
        return mod.TeknoparkCollector().collect(cities)
    if source == "bilisim500":
        mod = _load("stajbot.collectors.bilisim500")
        return mod.collect(cities)
    if source == "google_places":
        mod = _load("stajbot.collectors.google_places")
        return mod.GooglePlacesCollector().collect(cities)
    raise ValueError(f"Bilinmeyen kaynak: {source}")


def _ingest(raws: Iterable[RawCompany], session, *, allow_non_target: bool = False) -> Any:
    ingest = _load("stajbot.collectors.ingest")
    return ingest.ingest(raws, session, allow_non_target=allow_non_target)


# --- CSV ------------------------------------------------------------------------------

def csv_template_text() -> str:
    """Şablon CSV içeriği (başlık + örnek satır)."""
    header = ",".join(CSV_TEMPLATE_COLUMNS)
    example = (
        "Örnek Yazılım A.Ş.,https://www.ornekyazilim.com,kariyer@ornekyazilim.com,İstanbul,Maslak,"
        ",0212 000 00 00,Fintech,Ödeme sistemleri geliştirir,,\"fintech;odeme\",Okul listesinden"
    )
    return header + "\n" + example + "\n"


def _fold_header(h: str) -> str:
    from .enums import ascii_fold

    return ascii_fold(h or "").replace("_", " ").replace("-", " ").strip()


def _split_tags(val: str | None) -> list[str]:
    if not val:
        return []
    parts = [p.strip() for p in val.replace(";", ",").replace("|", ",").split(",")]
    return [p for p in parts if p]


def read_csv_rows(path: Path, *, source: str = "csv", default_city: str | None = None) -> list[RawCompany]:
    """Şablon CSV'yi okur ve RawCompany listesi üretir (csv_import modülü yoksa kullanılan yerel okuyucu).

    Ayraç otomatik tespit edilir (virgül / noktalı virgül / sekme); BOM'lu UTF-8 kabul edilir.
    Başlıklar İngilizce ya da Türkçe olabilir (``_CSV_ALIASES``). Adı boş satırlar atlanır.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        return []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if not reader.fieldnames:
        return []
    colmap: dict[str, str] = {}
    for h in reader.fieldnames:
        key = _CSV_ALIASES.get(_fold_header(h), None) or _CSV_ALIASES.get(_fold_header(h).replace(" ", "_"))
        if key:
            colmap[h] = key
    if "name" not in colmap.values():
        raise ValueError(
            f"CSV'de şirket adı sütunu bulunamadı. Beklenen başlıklar: {', '.join(CSV_TEMPLATE_COLUMNS)}"
        )
    out: list[RawCompany] = []
    for i, row in enumerate(reader, start=2):
        rec: dict[str, str | None] = {}
        for h, key in colmap.items():
            rec[key] = clean_text(row.get(h))
        name = rec.get("name")
        if not name:
            log.debug("CSV satır %d: ad boş, atlandı", i)
            continue
        emails = [e.strip().lower() for e in (rec.get("email") or "").replace(";", ",").split(",") if e.strip()]
        emails = [e for e in emails if is_valid_email(e)]
        tags = _split_tags(rec.get("tags"))
        if source == "school":
            tags.append("okul-protokol")
        notes = rec.get("notes")
        raw: dict[str, Any] = {"row": i, "file": path.name}
        if notes:
            raw["notes"] = notes
        if rec.get("contact_name"):
            raw["contact_name"] = rec["contact_name"]
        out.append(
            RawCompany(
                name=name,
                source=source,
                source_url=f"file://{path.as_posix()}",
                website=normalize_url(rec.get("website")),
                city=rec.get("city") or default_city,
                district=rec.get("district"),
                address=rec.get("address"),
                phone=rec.get("phone"),
                sector=rec.get("sector"),
                description=rec.get("description"),
                tags=tags,
                emails=emails,
                school_protocol=(source == "school"),
                raw=raw,
            )
        )
    return out


def _read_import_file(path: Path, *, source: str, default_city: str | None) -> Iterable[RawCompany]:
    """Varsa ``collectors.csv_import`` modülünü kullanır, yoksa yerel okuyucuya düşer."""
    try:
        mod = importlib.import_module("stajbot.collectors.csv_import")
    except ModuleNotFoundError:
        mod = None
    if mod is not None:
        for fname in ("read_file", "read_csv", "parse_file", "parse_csv", "iter_file", "collect"):
            fn = getattr(mod, fname, None)
            if callable(fn):
                try:
                    return fn(path, source=source, default_city=default_city)
                except TypeError:
                    try:
                        return fn(path, source=source)
                    except TypeError:
                        return fn(path)
        log.warning("csv_import modülünde bilinen bir okuma fonksiyonu yok; yerel CSV okuyucu kullanılıyor.")
    if path.suffix.lower() in (".xlsx", ".xlsm") and mod is None:
        raise ValueError("XLSX içe aktarma için csv_import modülü gerekli; dosyayı CSV olarak kaydedin.")
    return read_csv_rows(path, source=source, default_city=default_city)


# --------------------------------------------------------------------------------------
# Komutlar
# --------------------------------------------------------------------------------------

def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"stajbot {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(False, "--version", help="Sürümü yaz ve çık.", callback=_version_callback, is_eager=True),
) -> None:
    """Staj başvuru otomasyonu komutları."""


@app.command("init-db")
@_cli_command
def init_db_cmd() -> None:
    """Veritabanı tablolarını oluşturur (varsa dokunmaz)."""
    settings = get_settings()
    typer.echo(f"Veritabanı hazır: {settings.db_url}")


@app.command("collect")
@_cli_command
def collect_cmd(
    source: Source = typer.Option(Source.all, "--source", "-s", help="Kaynak: argem | teknopark | bilisim500 | google_places | all"),
    city: Optional[list[str]] = typer.Option(None, "--city", "-c", help="Şehir (tekrarlanabilir). Boşsa .env'deki hedef şehirler."),
    allow_non_target: bool = typer.Option(False, "--allow-non-target", help="Hedef şehir dışındaki şirketleri de kaydet."),
) -> None:
    """Şirketleri kaynaklardan toplar ve veritabanına kaydeder (tekilleştirerek).

    Not: Ar-Ge merkezi listesi için XLSX dosyalarını tarayıcıyla indirip data/argem/ klasörüne koyabilirsiniz
    (sanayi.gov.tr otomatik indirmeyi engelleyebilir). Bilişim 500 CSV'si data/manual/ altında aranır.
    """
    cities = _cities_or_default(city)
    sources = list(COLLECT_SOURCES) if source == Source.all else [source.value]
    typer.echo(f"Toplama başlıyor — kaynaklar: {', '.join(sources)}; şehirler: {', '.join(cities)}")
    ok = 0
    failed: list[str] = []
    totals = {"created": 0, "updated": 0, "skipped_city": 0, "skipped_invalid": 0}
    for src in sources:
        typer.echo(f"\n== {src} ==")
        try:
            raws = _collector_for(src, cities)
            with db.get_session() as session:
                report = _ingest(raws, session, allow_non_target=allow_non_target)
            typer.echo(_fmt_ingest_report(src, report))
            for k in totals:
                totals[k] += int(_get(report, k, 0) or 0)
            ok += 1
        except Exception as e:  # noqa: BLE001 - bir kaynak düşerse diğerleriyle devam
            log.debug("Kaynak başarısız: %s", src, exc_info=True)
            failed.append(src)
            _warn(f"[{src}] kaynağı başarısız oldu, diğer kaynaklarla devam ediliyor: {type(e).__name__}: {e}")
    typer.echo(
        f"\nToplam — yeni: {totals['created']}, güncellenen: {totals['updated']}, "
        f"hedef dışı: {totals['skipped_city']}, geçersiz: {totals['skipped_invalid']}; "
        f"başarılı kaynak: {ok}/{len(sources)}"
    )
    if failed:
        _warn(f"Başarısız kaynaklar: {', '.join(failed)}")
    if ok == 0:
        raise typer.Exit(code=1)


@app.command("import-csv")
@_cli_command
def import_csv_cmd(
    path: Optional[Path] = typer.Argument(None, help="İçe aktarılacak CSV (UTF-8) ya da XLSX dosyası."),
    source: CsvSource = typer.Option(CsvSource.csv, "--source", "-s", help="csv: elle liste | school: okul protokol listesi (school_protocol=True)"),
    city: Optional[str] = typer.Option(None, "--city", "-c", help="Satırda şehir yoksa kullanılacak varsayılan şehir."),
    allow_non_target: bool = typer.Option(False, "--allow-non-target", help="Hedef şehir dışındaki şirketleri de kaydet."),
    print_template: bool = typer.Option(False, "--print-template", help="Şablon CSV başlığını yazdır ve çık."),
) -> None:
    """CSV/XLSX dosyasından şirket içe aktarır (okul protokol listesi, elle toplanan liste)."""
    if print_template:
        typer.echo(csv_template_text(), nl=False)
        return
    if path is None:
        _err("Dosya yolu gerekli. Şablon için: python -m stajbot import-csv --print-template")
        raise typer.Exit(code=2)
    if not path.exists():
        _err(f"Dosya bulunamadı: {path}")
        raise typer.Exit(code=2)
    default_city = normalize_city(city) if city else None
    raws = list(_read_import_file(path, source=source.value, default_city=default_city))
    typer.echo(f"{path.name}: {len(raws)} satır okundu (kaynak: {source.value}).")
    if not raws:
        _warn("İçe aktarılacak satır yok.")
        return
    with db.get_session() as session:
        report = _ingest(raws, session, allow_non_target=allow_non_target)
    typer.echo(_fmt_ingest_report(source.value, report))


def _select_companies_for_enrich(session, *, only_pending: bool, company_id: int | None, limit: int | None) -> list[Company]:
    if company_id is not None:
        c = session.get(Company, company_id)
        if c is None:
            raise ValueError(f"Şirket bulunamadı: id={company_id}")
        return [c]
    stmt = select(Company).where(Company.blacklisted.is_(False))
    if only_pending:
        stmt = stmt.where(Company.enriched_at.is_(None))
    # Onaylılar ve puanı yüksekler önce; en eski zenginleştirme en önce
    stmt = stmt.order_by(
        (Company.review_status == ReviewStatus.approved).desc(),
        Company.enriched_at.is_(None).desc(),
        Company.score.desc(),
        Company.id.asc(),
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


@app.command("enrich")
@_cli_command
def enrich_cmd(
    limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1, help="En çok kaç şirket."),
    only_pending: bool = typer.Option(False, "--only-pending", help="Yalnızca hiç zenginleştirilmemiş şirketler."),
    company_id: Optional[int] = typer.Option(None, "--company-id", help="Tek bir şirket."),
) -> None:
    """Şirket web sitelerini tarar: e-posta, teknoloji, kariyer sayfası, büyüklük ipucu."""
    website = _load("stajbot.enrich.website")
    done = 0
    failed = 0
    found_emails = 0
    with db.get_session() as session:
        companies = _select_companies_for_enrich(session, only_pending=only_pending, company_id=company_id, limit=limit)
        typer.echo(f"{len(companies)} şirket zenginleştirilecek.")
        for c in companies:
            try:
                result = website.enrich_company(c, session)
                session.commit()
                n_mail = len(_get(result, "emails", []) or [])
                found_emails += n_mail
                techs = _get(result, "tech_stack", None) or _get(result, "technologies", None) or []
                ok = _get(result, "ok", True)
                err = _get(result, "error", None)
                status = "tamam" if ok else f"hata: {err}"
                typer.echo(f"  [{c.id}] {c.name} — {status}; e-posta: {n_mail}, teknoloji: {len(techs)}")
                done += 1
            except Exception as e:  # noqa: BLE001 - tek şirket düşerse akış durmaz
                session.rollback()
                failed += 1
                log.debug("Zenginleştirme hatası [%s]", c.id, exc_info=True)
                _warn(f"[{c.id}] {c.name}: {type(e).__name__}: {e}")
    typer.echo(f"Zenginleştirme bitti — işlenen: {done}, hatalı: {failed}, bulunan e-posta: {found_emails}")


def _profile_techs(scoring: Any) -> list[str] | None:
    """Profil teknolojilerini scoring/llm modülünden okumayı dener; yoksa None."""
    for fname in ("profile_techs", "load_profile_techs", "profile_technologies"):
        fn = getattr(scoring, fname, None)
        if callable(fn):
            try:
                val = fn()
                return list(val) if val else None
            except Exception as e:  # noqa: BLE001
                log.warning("Profil teknolojileri okunamadı: %s", e)
                return None
    return None


@app.command("score")
@_cli_command
def score_cmd() -> None:
    """Tüm şirketler için otomatik puanı (0-100) yeniden hesaplar."""
    scoring = _load("stajbot.enrich.scoring")
    profile_techs = _profile_techs(scoring)
    changed = 0
    total = 0
    buckets = {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
    with db.get_session() as session:
        for c in session.scalars(select(Company)):
            total += 1
            new = int(scoring.compute_score(c, profile_techs))
            if new != c.score:
                c.score = new
                changed += 1
            idx = min(new // 20, 4)
            buckets[list(buckets)[idx]] += 1
    typer.echo(f"Puanlama bitti — şirket: {total}, değişen: {changed}")
    typer.echo("Dağılım: " + ", ".join(f"{k}: {v}" for k, v in buckets.items()))


def _ready_companies(session, limit: int | None) -> list[Company]:
    stmt = (
        select(Company)
        .where(
            Company.status == AppStatus.ready,
            Company.review_status == ReviewStatus.approved,
            Company.blacklisted.is_(False),
        )
        .order_by(Company.score.desc(), Company.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


@app.command("compose")
@_cli_command
def compose_cmd(
    company_id: Optional[int] = typer.Option(None, "--company-id", help="Tek bir şirket için taslak üret."),
    all_ready: bool = typer.Option(False, "--all-ready", help="Durumu 'ready' olan tüm onaylı şirketler."),
    no_llm: bool = typer.Option(False, "--no-llm", help="LLM paragrafı yerine şablonun sabit paragrafını kullan."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1, help="En çok kaç şirket (--all-ready ile)."),
) -> None:
    """Şablon + LLM ile kişiselleştirilmiş başvuru maili taslağı üretir (gönderilmez, onay bekler)."""
    if company_id is None and not all_ready:
        _err("--company-id N ya da --all-ready vermelisiniz.")
        raise typer.Exit(code=2)
    compose = _load("stajbot.mail.compose")
    use_llm = not no_llm
    if use_llm and not get_settings().llm_configured:
        _warn("ANTHROPIC_API_KEY tanımlı değil; şablonun sabit paragrafı kullanılacak.")
    ok_n = 0
    fail_n = 0
    with db.get_session() as session:
        if company_id is not None:
            c = session.get(Company, company_id)
            if c is None:
                raise ValueError(f"Şirket bulunamadı: id={company_id}")
            companies = [c]
        else:
            companies = _ready_companies(session, limit)
        typer.echo(f"{len(companies)} şirket için taslak üretilecek (LLM: {'açık' if use_llm else 'kapalı'}).")
        for c in companies:
            try:
                res = compose.compose_for_company(c, session, use_llm=use_llm)
                session.commit()
                if _get(res, "ok", True):
                    msg = _get(res, "message", None)
                    to_addr = _get(msg, "to_addr", None) or (c.primary_contact.email if c.primary_contact else "?")
                    tmpl = _get(res, "template_name", None) or _get(msg, "template_name", None) or ""
                    typer.echo(f"  [{c.id}] {c.name} -> {to_addr} ({tmpl}) taslak hazır")
                    ok_n += 1
                else:
                    fail_n += 1
                    _warn(f"[{c.id}] {c.name}: {_get(res, 'error', 'taslak üretilemedi')}")
            except Exception as e:  # noqa: BLE001
                session.rollback()
                fail_n += 1
                log.debug("Taslak üretim hatası [%s]", c.id, exc_info=True)
                _warn(f"[{c.id}] {c.name}: {type(e).__name__}: {e}")
    typer.echo(f"Taslak üretimi bitti — başarılı: {ok_n}, başarısız: {fail_n}. Onay için web arayüzü: /mailler")


def _fmt_send_report(report: Any, dry_run: bool) -> str:
    sent = _get(report, "sent", 0)
    skipped = _get(report, "skipped", 0)
    failed = _get(report, "failed", 0)
    errors = list(_get(report, "errors", []) or [])
    mode = _get(report, "mode", None) or get_settings().mail_mode
    head = "Prova (dry-run)" if dry_run else "Gönderim"
    line = f"{head} bitti — mod: {mode}; gönderilen: {sent}, atlanan: {skipped}, hatalı: {failed}"
    reason = _get(report, "reason", None) or _get(report, "note", None)
    if reason:
        line += f"\n  Not: {reason}"
    if errors:
        line += "\n" + "\n".join(f"  - {e}" for e in errors[:10])
    return line


@app.command("send")
@_cli_command
def send_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Gönderme; ne yapılacağını göster."),
    force: bool = typer.Option(False, "--force", help="Mesai saati / hafta içi kontrolünü aş."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1, help="En çok kaç mail."),
) -> None:
    """Onaylanmış taslakları gönderir (MAIL_MODE: dry | draft | send); günlük limit ve bekleme uygular."""
    sender = _load("stajbot.mail.sender")
    settings = get_settings()
    if not dry_run and settings.mail_mode != "dry" and not settings.mail_configured:
        _err("MAIL_USER / MAIL_PASSWORD tanımlı değil (.env). Gmail uygulama şifresi gerekir.")
        raise typer.Exit(code=2)
    with db.get_session() as session:
        pending = session.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.status == MailStatus.approved)) or 0
        typer.echo(f"Onaylı bekleyen mail: {pending}; mod: {settings.mail_mode}; günlük limit: {settings.daily_send_limit}")
        report = sender.send_approved(session, dry_run=dry_run, force=force, limit=limit)
    typer.echo(_fmt_send_report(report, dry_run))


@app.command("inbox")
@_cli_command
def inbox_cmd() -> None:
    """Gelen kutusunu (IMAP) okur, yeni mesajları kaydeder ve şirketlere eşleştirir."""
    inbox = _load("stajbot.mail.inbox")
    settings = get_settings()
    if not settings.mail_configured:
        _err("MAIL_USER / MAIL_PASSWORD tanımlı değil (.env); IMAP bağlantısı kurulamaz.")
        raise typer.Exit(code=2)
    with db.get_session() as session:
        msgs = list(inbox.fetch_new(session) or [])
        matched = [m for m in msgs if _get(m, "company_id", None) is not None]
        for m in msgs:
            comp = _get(m, "company", None)
            who = f"-> [{comp.id}] {comp.name}" if comp is not None else "-> sahipsiz"
            typer.echo(f"  {_get(m, 'from_addr', '?')} | {_get(m, 'subject', '(konu yok)')} {who}")
    typer.echo(f"Gelen kutusu: {len(msgs)} yeni mesaj, eşleşen: {len(matched)}, sahipsiz: {len(msgs) - len(matched)}")


@app.command("followups")
@_cli_command
def followups_cmd(
    limit: Optional[int] = typer.Option(None, "--limit", "-n", min=1, help="En çok kaç hatırlatma taslağı."),
) -> None:
    """Süresi dolanları 'cevap yok' yapar ve hatırlatma maili taslakları üretir (onay bekler)."""
    compose = _load("stajbot.mail.compose")
    marked = 0
    drafted = 0
    skipped = 0
    failed = 0
    with db.get_session() as session:
        marked = services.mark_no_reply(session)
        session.commit()
        stmt = (
            select(Company)
            .where(Company.status == AppStatus.no_reply, Company.blacklisted.is_(False))
            .order_by(Company.next_followup_at.asc().nulls_last(), Company.id.asc())
        )
        companies = list(session.scalars(stmt))
        for c in companies:
            has_draft = any(
                m.direction == Direction.outbound and m.status in (MailStatus.draft, MailStatus.approved, MailStatus.queued)
                for m in c.emails
            )
            if has_draft:
                skipped += 1
                continue
            if limit and drafted >= limit:
                break
            try:
                res = compose.compose_followup(c, session)
                session.commit()
                if _get(res, "ok", True):
                    drafted += 1
                    typer.echo(f"  [{c.id}] {c.name}: hatırlatma taslağı üretildi")
                else:
                    failed += 1
                    _warn(f"[{c.id}] {c.name}: {_get(res, 'error', 'taslak üretilemedi')}")
            except Exception as e:  # noqa: BLE001
                session.rollback()
                failed += 1
                log.debug("Hatırlatma taslağı hatası [%s]", c.id, exc_info=True)
                _warn(f"[{c.id}] {c.name}: {type(e).__name__}: {e}")
    typer.echo(
        f"Hatırlatma — 'cevap yok' yapılan: {marked}, taslak üretilen: {drafted}, "
        f"zaten taslağı olan: {skipped}, hatalı: {failed}"
    )


@app.command("serve")
@_cli_command
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host", help="Dinlenecek adres."),
    port: int = typer.Option(8000, "--port", "-p", help="Port."),
    reload: bool = typer.Option(False, "--reload", help="Kod değişince yeniden başlat (geliştirme)."),
) -> None:
    """Web arayüzünü başlatır (FastAPI + HTMX)."""
    try:
        import uvicorn
    except ModuleNotFoundError as e:  # pragma: no cover
        raise RuntimeError("uvicorn kurulu değil (requirements.txt).") from e
    _load("stajbot.web.app", hint="Web arayüzü modülü (stajbot/web/app.py) eksik.")
    typer.echo(f"Web arayüzü: http://{host}:{port}  (durdurmak için Ctrl+C)")
    uvicorn.run("stajbot.web.app:app", host=host, port=port, reload=reload, log_level="info")


@app.command("stats")
@_cli_command
def stats_cmd() -> None:
    """Hızlı özet: şirket sayıları, inceleme kuyruğu, mailler, cevaplar."""
    now = now_utc()
    today_start = datetime(now.year, now.month, now.day)
    with db.get_session() as session:
        total = session.scalar(select(func.count()).select_from(Company)) or 0
        by_status = services.companies_by_status(session)
        review_rows = session.execute(select(Company.review_status, func.count()).group_by(Company.review_status)).all()
        review = {r: 0 for r in ReviewStatus}
        for r, n in review_rows:
            review[r] = n
        with_email = session.scalar(select(func.count(func.distinct(Contact.company_id)))) or 0
        enriched = session.scalar(select(func.count()).select_from(Company).where(Company.enriched_at.is_not(None))) or 0
        blacklisted = session.scalar(select(func.count()).select_from(Company).where(Company.blacklisted.is_(True))) or 0
        city_rows = session.execute(
            select(Company.city, func.count()).group_by(Company.city).order_by(func.count().desc())
        ).all()
        mail_rows = session.execute(select(EmailMessage.status, func.count()).group_by(EmailMessage.status)).all()
        mails = {m: 0 for m in MailStatus}
        for st, n in mail_rows:
            mails[st] = n
        sent_today = session.scalar(
            select(func.count()).select_from(EmailMessage).where(
                EmailMessage.direction == Direction.outbound,
                EmailMessage.status == MailStatus.sent,
                EmailMessage.sent_at.is_not(None),
                EmailMessage.sent_at >= today_start,
            )
        ) or 0
        unmatched = session.scalar(
            select(func.count()).select_from(EmailMessage).where(
                EmailMessage.direction == Direction.inbound, EmailMessage.matched_by == MatchMethod.none
            )
        ) or 0
        upcoming = session.scalar(
            select(func.count()).select_from(Company).where(
                Company.status == AppStatus.sent,
                Company.next_followup_at.is_not(None),
                Company.next_followup_at <= now + timedelta(days=3),
            )
        ) or 0

    typer.echo(f"Şirket: {total} (e-postası olan: {with_email}, zenginleştirilen: {enriched}, kara liste: {blacklisted})")
    typer.echo(
        "İnceleme: "
        + ", ".join(f"{label}: {review[r]}" for r, label in (
            (ReviewStatus.pending, "bekleyen"), (ReviewStatus.approved, "onaylı"),
            (ReviewStatus.rejected, "reddedilen"), (ReviewStatus.later, "sonra"),
        ))
    )
    typer.echo("Durumlar:")
    for st in AppStatus:
        n = by_status.get(st, 0)
        if n:
            typer.echo(f"  {APP_STATUS_LABELS[st]:<24} {n}")
    if city_rows:
        typer.echo("Şehirler: " + ", ".join(f"{c or 'bilinmiyor'}: {n}" for c, n in city_rows[:12]))
    typer.echo(
        f"Mailler — taslak: {mails[MailStatus.draft]}, onaylı: {mails[MailStatus.approved]}, "
        f"kuyrukta: {mails[MailStatus.queued]}, gönderilen: {mails[MailStatus.sent]}, "
        f"hatalı: {mails[MailStatus.failed]}, gelen: {mails[MailStatus.received]}"
    )
    typer.echo(f"Bugün gönderilen: {sent_today}; sahipsiz gelen mail: {unmatched}; 3 gün içinde hatırlatma: {upcoming}")
    sent_like = sum(by_status.get(s, 0) for s in (
        AppStatus.sent, AppStatus.no_reply, AppStatus.followup_sent, AppStatus.replied, AppStatus.positive,
        AppStatus.negative, AppStatus.info_requested, AppStatus.phone_call, AppStatus.interview,
        AppStatus.presentation, AppStatus.offer, AppStatus.accepted,
    ))
    replied_like = sum(by_status.get(s, 0) for s in (
        AppStatus.replied, AppStatus.positive, AppStatus.negative, AppStatus.info_requested, AppStatus.phone_call,
        AppStatus.interview, AppStatus.presentation, AppStatus.offer, AppStatus.accepted,
    ))
    if sent_like:
        typer.echo(f"Cevap oranı: {replied_like}/{sent_like} ({100 * replied_like / sent_like:.0f}%)")


def main() -> None:  # pragma: no cover - giriş noktası
    try:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
