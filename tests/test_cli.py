"""CLI testleri: typer CliRunner + bellek içi veritabanı.

Diğer ajanların modülleri (ingest, compose, sender, website, scoring) bu testlerde sahte (fake)
modüllerle değiştirilir; böylece CLI'nin bağlama/raporlama davranışı o modüllerden bağımsız
doğrulanır. Gerçek modül mevcutsa ayrıca `TestRealModules` sınıfı onlarla uçtan uca çalışır.
"""
from __future__ import annotations

import importlib
import sys
import types
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from stajbot import db, services
from stajbot.cli import CSV_TEMPLATE_COLUMNS, app, read_csv_rows
from stajbot.collectors.base import RawCompany
from stajbot.enums import Actor, AppStatus, Direction, MailStatus, ReviewStatus, is_target_city, normalize_city
from stajbot.models import Company, Contact, EmailMessage, Event
from stajbot.utils import domain_of, normalize_name, now_utc

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
TEMPLATE_CSV = FIXTURES / "sablon.csv"


# --------------------------------------------------------------------------------------
# Fikstürler
# --------------------------------------------------------------------------------------

@pytest.fixture()
def cli_db(engine, monkeypatch):
    """CLI'nin db.get_session() çağrısını bellek içi engine'e yönlendirir."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "get_sessionmaker", lambda eng=None: factory if eng is None else sessionmaker(bind=eng, expire_on_commit=False, future=True))
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", factory)
    return engine


@pytest.fixture()
def runner():
    return CliRunner()


def _fake_module(monkeypatch, name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _ingest_report(**kw):
    base = dict(created=0, updated=0, skipped_city=0, skipped_invalid=0, errors=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _simple_ingest(raws, session, *, allow_non_target=False):
    """MIMARI §6'ya uygun, küçük bir ingest taklidi (domain ya da ad+şehir ile eşleştirir)."""
    rep = _ingest_report()
    for r in raws:
        if not r.name or not r.name.strip():
            rep.skipped_invalid += 1
            continue
        city = normalize_city(r.city)
        if not allow_non_target and not is_target_city(city):
            rep.skipped_city += 1
            continue
        dom = domain_of(r.website) or (domain_of(r.emails[0]) if r.emails else None)
        existing = None
        if dom:
            existing = session.scalar(select(Company).where(Company.domain == dom))
        if existing is None:
            for c in session.scalars(select(Company).where(Company.city == city)):
                if normalize_name(c.name) == normalize_name(r.name):
                    existing = c
                    break
        if existing is None:
            existing = Company(name=r.name, domain=dom, website=r.website, city=city, district=r.district,
                               sector=r.sector, description=r.description, tags=list(r.tags),
                               school_protocol=r.school_protocol)
            session.add(existing)
            session.flush()
            rep.created += 1
        else:
            existing.school_protocol = existing.school_protocol or r.school_protocol
            rep.updated += 1
        for e in r.emails:
            services.add_contact(session, existing, e, source=r.source)
    return rep


@pytest.fixture()
def fake_ingest(monkeypatch):
    calls: list[dict] = []

    def ingest(raws, session, *, allow_non_target=False):
        raws = list(raws)
        calls.append({"n": len(raws), "allow_non_target": allow_non_target, "raws": raws})
        return _simple_ingest(raws, session, allow_non_target=allow_non_target)

    _fake_module(monkeypatch, "stajbot.collectors.ingest", ingest=ingest)
    # csv_import modülü varsa bile yerel okuyucu kullanılsın (belirleyici test)
    monkeypatch.setitem(sys.modules, "stajbot.collectors.csv_import", None)
    return calls


@pytest.fixture()
def fake_compose(monkeypatch):
    calls: list[dict] = []

    def compose_for_company(company, session, *, use_llm=True):
        calls.append({"company_id": company.id, "use_llm": use_llm, "kind": "apply"})
        contact = company.primary_contact
        if contact is None:
            return SimpleNamespace(ok=False, error="birincil e-posta yok", message=None)
        for m in list(company.emails):
            if m.direction == Direction.outbound and m.status == MailStatus.draft:
                session.delete(m)
        msg = EmailMessage(company=company, direction=Direction.outbound, status=MailStatus.draft,
                           to_addr=contact.email, subject=f"Uzun Dönem Staj Başvurusu - Test [{company.tracking_code}]",
                           body_text="test", template_name="default", tracking_code=company.tracking_code)
        session.add(msg)
        if company.status == AppStatus.ready:
            services.change_status(session, company, AppStatus.drafted, actor=Actor.system)
        session.flush()
        return SimpleNamespace(ok=True, error=None, message=msg, template_name="default")

    def compose_followup(company, session):
        calls.append({"company_id": company.id, "kind": "followup"})
        contact = company.primary_contact
        msg = EmailMessage(company=company, direction=Direction.outbound, status=MailStatus.draft,
                           to_addr=contact.email if contact else None, subject="Hatırlatma", body_text="takip",
                           template_name="followup", tracking_code=company.tracking_code)
        session.add(msg)
        session.flush()
        return SimpleNamespace(ok=True, error=None, message=msg, template_name="followup")

    _fake_module(monkeypatch, "stajbot.mail.compose", compose_for_company=compose_for_company, compose_followup=compose_followup)
    return calls


@pytest.fixture()
def fake_sender(monkeypatch):
    calls: list[dict] = []

    def send_approved(session, *, dry_run=False, force=False, limit=None):
        calls.append({"dry_run": dry_run, "force": force, "limit": limit})
        n = session.scalar(select(EmailMessage.id).where(EmailMessage.status == MailStatus.approved))
        return SimpleNamespace(sent=0, skipped=1 if n else 0, failed=0, errors=[], mode="dry",
                               reason="prova: gönderim yapılmadı" if dry_run else None)

    _fake_module(monkeypatch, "stajbot.mail.sender", send_approved=send_approved)
    return calls


def _seed_ready_company(engine) -> int:
    """conftest `company` fikstürünün CLI engine'i üzerinde eşleniği (status: ready)."""
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        c = Company(name="Örnek Yazılım A.Ş.", domain="ornekyazilim.com", website="https://www.ornekyazilim.com",
                    city="İstanbul", sector="Fintech", tech_stack=["Python", "React"], review_status=ReviewStatus.approved)
        s.add(c)
        s.flush()
        services.add_contact(s, c, "kariyer@ornekyazilim.com", source="test")
        s.commit()
        assert c.status == AppStatus.ready
        return c.id


def _one(engine, model, **where):
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        stmt = select(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return s.scalars(stmt).all()


# --------------------------------------------------------------------------------------
# Temel komutlar
# --------------------------------------------------------------------------------------

def test_version(runner):
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert "stajbot" in r.output


def test_no_args_shows_help(runner, cli_db):
    r = runner.invoke(app, [])
    assert "init-db" in r.output and "collect" in r.output


def test_init_db(runner, cli_db):
    r = runner.invoke(app, ["init-db"])
    assert r.exit_code == 0, r.output
    assert "Veritabanı hazır" in r.output
    with cli_db.connect() as conn:
        from sqlalchemy import inspect

        assert "companies" in inspect(conn).get_table_names()


def test_stats_empty(runner, cli_db):
    r = runner.invoke(app, ["stats"])
    assert r.exit_code == 0, r.output
    assert "Şirket: 0" in r.output
    assert "Mailler" in r.output


def test_stats_with_company(runner, cli_db):
    _seed_ready_company(cli_db)
    r = runner.invoke(app, ["stats"])
    assert r.exit_code == 0, r.output
    assert "Şirket: 1 (e-postası olan: 1" in r.output
    assert "Hazır" in r.output
    assert "İstanbul: 1" in r.output
    assert "onaylı: 1" in r.output


# --------------------------------------------------------------------------------------
# CSV içe aktarma
# --------------------------------------------------------------------------------------

def test_print_template(runner, cli_db):
    r = runner.invoke(app, ["import-csv", "--print-template"])
    assert r.exit_code == 0
    assert r.output.splitlines()[0] == ",".join(CSV_TEMPLATE_COLUMNS)


def test_read_csv_rows_template():
    rows = read_csv_rows(TEMPLATE_CSV, source="csv")
    # boş adlı satır düşer, şehirsiz satır kalır (ingest karar verir)
    assert [r.name for r in rows] == ["Örnek Yazılım A.Ş.", "Konya Bilişim Ltd.", "Şehirsiz Firma"]
    first = rows[0]
    assert first.emails == ["kariyer@ornekyazilim.com"]
    assert first.website == "https://www.ornekyazilim.com"
    assert first.tags == ["fintech", "odeme"]
    assert first.raw["notes"] == "Okul listesinden"
    assert first.school_protocol is False
    second = rows[1]
    assert second.website == "https://konyabilisim.com.tr"
    assert second.raw["contact_name"] == "Ayşe Yılmaz"
    assert rows[2].city is None


def test_read_csv_rows_turkish_headers_semicolon(tmp_path):
    p = tmp_path / "liste.csv"
    p.write_text("Şirket Adı;Web Sitesi;E-posta;Şehir\nAcme Ltd. Şti.;acme.com.tr;ik@acme.com.tr;Ankara\n", encoding="utf-8")
    rows = read_csv_rows(p, source="school", default_city="Konya")
    assert len(rows) == 1
    r = rows[0]
    assert r.name == "Acme Ltd. Şti." and r.city == "Ankara" and r.emails == ["ik@acme.com.tr"]
    assert r.school_protocol is True and "okul-protokol" in r.tags


def test_read_csv_rows_default_city(tmp_path):
    p = tmp_path / "liste.csv"
    p.write_text("name,email\nFoo,info@foo.com\n", encoding="utf-8")
    rows = read_csv_rows(p, default_city="Konya")
    assert rows[0].city == "Konya"


def test_read_csv_rows_missing_name_column(tmp_path):
    p = tmp_path / "liste.csv"
    p.write_text("email,city\ninfo@foo.com,Konya\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_csv_rows(p)


def test_import_csv_template(runner, cli_db, fake_ingest):
    r = runner.invoke(app, ["import-csv", str(TEMPLATE_CSV)])
    assert r.exit_code == 0, r.output
    assert "3 satır okundu" in r.output
    assert "[csv] yeni: 2" in r.output and "hedef dışı şehir: 1" in r.output
    assert fake_ingest[0]["allow_non_target"] is False
    companies = _one(cli_db, Company)
    assert {c.name for c in companies} == {"Örnek Yazılım A.Ş.", "Konya Bilişim Ltd."}
    assert all(c.school_protocol is False for c in companies)
    contacts = _one(cli_db, Contact)
    assert {c.email for c in contacts} == {"kariyer@ornekyazilim.com", "info@konyabilisim.com.tr"}


def test_import_csv_school_sets_protocol_and_default_city(runner, cli_db, fake_ingest):
    r = runner.invoke(app, ["import-csv", str(TEMPLATE_CSV), "--source", "school", "--city", "konya"])
    assert r.exit_code == 0, r.output
    raws = fake_ingest[0]["raws"]
    assert all(x.school_protocol for x in raws)
    assert all(x.source == "school" for x in raws)
    assert raws[2].city == "Konya"  # şehirsiz satır varsayılan şehri aldı
    assert "[school] yeni: 3" in r.output
    companies = _one(cli_db, Company)
    assert len(companies) == 3 and all(c.school_protocol for c in companies)


def test_import_csv_twice_is_idempotent(runner, cli_db, fake_ingest):
    runner.invoke(app, ["import-csv", str(TEMPLATE_CSV)])
    r = runner.invoke(app, ["import-csv", str(TEMPLATE_CSV)])
    assert r.exit_code == 0
    assert "yeni: 0" in r.output and "güncellenen: 2" in r.output
    assert len(_one(cli_db, Company)) == 2


def test_import_csv_allow_non_target(runner, cli_db, fake_ingest):
    r = runner.invoke(app, ["import-csv", str(TEMPLATE_CSV), "--allow-non-target"])
    assert r.exit_code == 0
    assert fake_ingest[0]["allow_non_target"] is True


def test_import_csv_missing_file(runner, cli_db, fake_ingest):
    r = runner.invoke(app, ["import-csv", "yok/boyle/dosya.csv"])
    assert r.exit_code == 2
    assert "Dosya bulunamadı" in r.output


def test_import_csv_no_path(runner, cli_db, fake_ingest):
    r = runner.invoke(app, ["import-csv"])
    assert r.exit_code == 2
    assert "Dosya yolu gerekli" in r.output


def test_import_csv_empty_file(runner, cli_db, fake_ingest, tmp_path):
    p = tmp_path / "bos.csv"
    p.write_text("name,email\n", encoding="utf-8")
    r = runner.invoke(app, ["import-csv", str(p)])
    assert r.exit_code == 0
    assert "İçe aktarılacak satır yok" in r.output
    assert fake_ingest == []


# --------------------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------------------

def test_collect_continues_when_source_fails(runner, cli_db, fake_ingest, monkeypatch):
    import stajbot.cli as cli

    seen: list[tuple[str, list[str]]] = []

    def fake_collector(source, cities):
        seen.append((source, cities))
        if source == "teknopark":
            raise RuntimeError("site erişilemez")
        if source == "argem":
            return iter([RawCompany(name="ArGe Firma", source="argem", city="Ankara", website="argefirma.com")])
        if source == "google_places":
            # tembel hata: iterasyon sırasında patlar
            def gen():
                yield RawCompany(name="GP Firma", source="google_places", city="İzmir", website="gpfirma.com")
                raise ValueError("kota bitti")
            return gen()
        return iter([])

    monkeypatch.setattr(cli, "_collector_for", fake_collector)
    r = runner.invoke(app, ["collect", "--city", "Ankara", "--city", "izmir"])
    assert r.exit_code == 0, r.output
    assert [s for s, _ in seen] == ["argem", "teknopark", "bilisim500", "google_places"]
    assert seen[0][1] == ["Ankara", "İzmir"]
    assert "[argem] yeni: 1" in r.output
    assert "[bilisim500] yeni: 0" in r.output
    assert "teknopark" in r.output and "UYARI" in r.output and "site erişilemez" in r.output
    assert "kota bitti" in r.output
    assert "başarılı kaynak: 2/4" in r.output
    assert {c.name for c in _one(cli_db, Company)} == {"ArGe Firma"}


def test_collect_single_source_all_fail_exits_nonzero(runner, cli_db, fake_ingest, monkeypatch):
    import stajbot.cli as cli

    def boom(source, cities):
        raise RuntimeError("çöktü")

    monkeypatch.setattr(cli, "_collector_for", boom)
    r = runner.invoke(app, ["collect", "--source", "argem"])
    assert r.exit_code == 1
    assert "Başarısız kaynaklar: argem" in r.output


def test_collect_default_cities_from_settings(runner, cli_db, fake_ingest, monkeypatch):
    import stajbot.cli as cli
    from stajbot.config import get_settings

    seen = {}

    def fake_collector(source, cities):
        seen[source] = cities
        return iter([])

    monkeypatch.setattr(cli, "_collector_for", fake_collector)
    r = runner.invoke(app, ["collect", "--source", "bilisim500"])
    assert r.exit_code == 0, r.output
    assert seen["bilisim500"] == list(get_settings().target_cities)


def test_collect_invalid_source(runner, cli_db):
    r = runner.invoke(app, ["collect", "--source", "linkedin"])
    assert r.exit_code != 0


def test_collector_dispatch_uses_real_collectors(monkeypatch):
    """_collector_for gerçek collector modüllerine doğru şekilde bağlanır (ağa çıkılmaz)."""
    import stajbot.cli as cli
    import stajbot.collectors.argem as argem
    import stajbot.collectors.bilisim500 as b500
    import stajbot.collectors.google_places as gp
    import stajbot.collectors.teknopark as tp

    monkeypatch.setattr(argem, "collect", lambda cities, **kw: iter([RawCompany(name="a", source="argem")]))
    monkeypatch.setattr(b500, "collect", lambda cities: iter([RawCompany(name="b", source="bilisim500")]))

    class _TP:
        def collect(self, cities=None):
            return iter([RawCompany(name="t", source="teknopark")])

    class _GP:
        def collect(self, cities=None):
            return iter([RawCompany(name="g", source="google_places")])

    monkeypatch.setattr(tp, "TeknoparkCollector", _TP)
    monkeypatch.setattr(gp, "GooglePlacesCollector", _GP)
    names = {src: [x.name for x in cli._collector_for(src, ["Ankara"])] for src in cli.COLLECT_SOURCES}
    assert names == {"argem": ["a"], "teknopark": ["t"], "bilisim500": ["b"], "google_places": ["g"]}
    with pytest.raises(ValueError):
        cli._collector_for("yok", [])


# --------------------------------------------------------------------------------------
# compose / send / followups / score / enrich
# --------------------------------------------------------------------------------------

def test_compose_requires_target(runner, cli_db, fake_compose):
    r = runner.invoke(app, ["compose"])
    assert r.exit_code == 2
    assert "--company-id" in r.output


def test_compose_no_llm_on_ready_company(runner, cli_db, fake_compose):
    cid = _seed_ready_company(cli_db)
    r = runner.invoke(app, ["compose", "--company-id", str(cid), "--no-llm"])
    assert r.exit_code == 0, r.output
    assert fake_compose == [{"company_id": cid, "use_llm": False, "kind": "apply"}]
    assert "kariyer@ornekyazilim.com" in r.output and "başarılı: 1, başarısız: 0" in r.output
    msgs = _one(cli_db, EmailMessage, company_id=cid)
    assert len(msgs) == 1 and msgs[0].status == MailStatus.draft
    comp = _one(cli_db, Company, id=cid)[0]
    assert comp.status == AppStatus.drafted


def test_compose_all_ready_with_limit(runner, cli_db, fake_compose):
    cid = _seed_ready_company(cli_db)
    Session = sessionmaker(bind=cli_db, expire_on_commit=False, future=True)
    with Session() as s:
        c2 = Company(name="Pending Firma", city="Ankara", review_status=ReviewStatus.pending)  # onaysız: seçilmez
        c3 = Company(name="Hazır İki", domain="hazir2.com", city="Konya", review_status=ReviewStatus.approved, score=90)
        s.add_all([c2, c3])
        s.flush()
        services.add_contact(s, c3, "ik@hazir2.com", source="test")
        s.commit()
        c3_id = c3.id
    r = runner.invoke(app, ["compose", "--all-ready", "--limit", "1"])
    assert r.exit_code == 0, r.output
    assert [c["company_id"] for c in fake_compose] == [c3_id]  # puanı yüksek olan önce
    r = runner.invoke(app, ["compose", "--all-ready"])
    assert r.exit_code == 0
    assert fake_compose[-1]["company_id"] == cid and fake_compose[-1]["use_llm"] is True


def test_compose_reports_failure_without_contact(runner, cli_db, fake_compose):
    Session = sessionmaker(bind=cli_db, expire_on_commit=False, future=True)
    with Session() as s:
        c = Company(name="Mailsiz", city="Konya", review_status=ReviewStatus.approved)
        s.add(c)
        s.commit()
        cid = c.id
    r = runner.invoke(app, ["compose", "--company-id", str(cid), "--no-llm"])
    assert r.exit_code == 0
    assert "birincil e-posta yok" in r.output and "başarısız: 1" in r.output


def test_compose_unknown_company_is_error(runner, cli_db, fake_compose):
    r = runner.invoke(app, ["compose", "--company-id", "999", "--no-llm"])
    assert r.exit_code == 1
    assert "HATA" in r.output and "Şirket bulunamadı" in r.output


def test_send_dry_run(runner, cli_db, fake_sender):
    cid = _seed_ready_company(cli_db)
    Session = sessionmaker(bind=cli_db, expire_on_commit=False, future=True)
    with Session() as s:
        s.add(EmailMessage(company_id=cid, direction=Direction.outbound, status=MailStatus.approved, to_addr="kariyer@ornekyazilim.com", subject="x"))
        s.commit()
    r = runner.invoke(app, ["send", "--dry-run", "--limit", "5"])
    assert r.exit_code == 0, r.output
    assert fake_sender == [{"dry_run": True, "force": False, "limit": 5}]
    assert "Onaylı bekleyen mail: 1" in r.output
    assert "Prova (dry-run) bitti" in r.output and "atlanan: 1" in r.output
    assert "prova: gönderim yapılmadı" in r.output


def test_send_force_passthrough(runner, cli_db, fake_sender):
    r = runner.invoke(app, ["send", "--dry-run", "--force"])
    assert r.exit_code == 0, r.output
    assert fake_sender[0]["force"] is True


def test_send_requires_credentials_when_mode_is_not_dry(runner, cli_db, fake_sender, monkeypatch):
    import stajbot.cli as cli

    fake_settings = SimpleNamespace(mail_mode="send", mail_configured=False, daily_send_limit=15)
    monkeypatch.setattr(cli, "get_settings", lambda: fake_settings)
    r = runner.invoke(app, ["send"])
    assert r.exit_code == 2
    assert "MAIL_USER" in r.output
    assert fake_sender == []


def test_followups_marks_and_drafts(runner, cli_db, fake_compose):
    cid = _seed_ready_company(cli_db)
    Session = sessionmaker(bind=cli_db, expire_on_commit=False, future=True)
    with Session() as s:
        c = s.get(Company, cid)
        c.status = AppStatus.sent
        c.next_followup_at = now_utc() - timedelta(days=1)
        s.commit()
    r = runner.invoke(app, ["followups"])
    assert r.exit_code == 0, r.output
    assert "'cevap yok' yapılan: 1" in r.output and "taslak üretilen: 1" in r.output
    assert fake_compose == [{"company_id": cid, "kind": "followup"}]
    comp = _one(cli_db, Company, id=cid)[0]
    assert comp.status == AppStatus.no_reply
    # ikinci çalıştırma: taslağı olan şirket için yeniden üretilmez
    r2 = runner.invoke(app, ["followups"])
    assert r2.exit_code == 0
    assert "'cevap yok' yapılan: 0" in r2.output and "taslak üretilen: 0" in r2.output and "zaten taslağı olan: 1" in r2.output
    assert len(fake_compose) == 1


def test_score_updates_companies(runner, cli_db, monkeypatch):
    cid = _seed_ready_company(cli_db)
    _fake_module(monkeypatch, "stajbot.enrich.scoring", compute_score=lambda c, profile_techs=None: 42)
    r = runner.invoke(app, ["score"])
    assert r.exit_code == 0, r.output
    assert "şirket: 1, değişen: 1" in r.output and "40-59: 1" in r.output
    assert _one(cli_db, Company, id=cid)[0].score == 42


def test_enrich_only_pending_and_errors(runner, cli_db, monkeypatch):
    cid = _seed_ready_company(cli_db)
    Session = sessionmaker(bind=cli_db, expire_on_commit=False, future=True)
    with Session() as s:
        done = Company(name="Zaten Zengin", city="Konya", enriched_at=now_utc())
        broken = Company(name="Bozuk Site", city="Ankara", website="https://bozuk.example")
        s.add_all([done, broken])
        s.commit()
        broken_id = broken.id
    seen: list[int] = []

    def enrich_company(company, session, client=None):
        seen.append(company.id)
        if company.id == broken_id:
            raise ConnectionError("bağlanamadı")
        company.enriched_at = now_utc()
        return SimpleNamespace(ok=True, emails=["ik@ornekyazilim.com"], tech_stack=["Python"], error=None)

    _fake_module(monkeypatch, "stajbot.enrich.website", enrich_company=enrich_company)
    r = runner.invoke(app, ["enrich", "--only-pending"])
    assert r.exit_code == 0, r.output
    assert sorted(seen) == sorted([cid, broken_id])
    assert "2 şirket zenginleştirilecek" in r.output
    assert "işlenen: 1, hatalı: 1, bulunan e-posta: 1" in r.output
    assert "bağlanamadı" in r.output
    # --company-id + --limit
    seen.clear()
    r = runner.invoke(app, ["enrich", "--company-id", str(cid)])
    assert r.exit_code == 0 and seen == [cid]
    seen.clear()
    r = runner.invoke(app, ["enrich", "--limit", "1"])
    assert r.exit_code == 0 and len(seen) == 1


def test_missing_module_gives_turkish_error(runner, cli_db, monkeypatch):
    monkeypatch.setitem(sys.modules, "stajbot.mail.sender", None)
    r = runner.invoke(app, ["send", "--dry-run"])
    assert r.exit_code == 1
    assert "HATA" in r.output and "modülü bulunamadı" in r.output


def test_unexpected_exception_is_reported(runner, cli_db, monkeypatch):
    import stajbot.cli as cli

    def boom(session):
        raise RuntimeError("beklenmedik")

    monkeypatch.setattr(cli.services, "companies_by_status", boom)
    r = runner.invoke(app, ["stats"])
    assert r.exit_code == 1
    assert "HATA: RuntimeError: beklenmedik" in r.output


def test_inbox_requires_credentials(runner, cli_db, monkeypatch):
    import stajbot.cli as cli

    _fake_module(monkeypatch, "stajbot.mail.inbox", fetch_new=lambda session: [])
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(mail_configured=False))
    r = runner.invoke(app, ["inbox"])
    assert r.exit_code == 2 and "IMAP" in r.output


def test_inbox_reports_matches(runner, cli_db, monkeypatch):
    import stajbot.cli as cli

    cid = _seed_ready_company(cli_db)

    def fetch_new(session):
        c = session.get(Company, cid)
        m1 = EmailMessage(company=c, direction=Direction.inbound, status=MailStatus.received, from_addr="ik@ornekyazilim.com", subject="Re: Staj")
        m2 = EmailMessage(direction=Direction.inbound, status=MailStatus.received, from_addr="x@y.com", subject="Spam")
        session.add_all([m1, m2])
        session.flush()
        return [m1, m2]

    _fake_module(monkeypatch, "stajbot.mail.inbox", fetch_new=fetch_new)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(mail_configured=True))
    r = runner.invoke(app, ["inbox"])
    assert r.exit_code == 0, r.output
    assert "2 yeni mesaj, eşleşen: 1, sahipsiz: 1" in r.output
    assert "Örnek Yazılım" in r.output and "sahipsiz" in r.output


def test_serve_invokes_uvicorn(runner, cli_db, monkeypatch):
    import uvicorn

    calls = {}
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: calls.update(target=target, **kw))
    _fake_module(monkeypatch, "stajbot.web.app", app=object())
    r = runner.invoke(app, ["serve", "--port", "9001", "--reload"])
    assert r.exit_code == 0, r.output
    assert calls["target"] == "stajbot.web.app:app" and calls["port"] == 9001 and calls["reload"] is True
    assert calls["host"] == "127.0.0.1"
    assert "http://127.0.0.1:9001" in r.output


# --------------------------------------------------------------------------------------
# Gerçek modüller mevcutsa uçtan uca (diğer ajanların modülleri indiğinde otomatik çalışır)
# --------------------------------------------------------------------------------------

def _has(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ModuleNotFoundError:
        return False


@pytest.mark.skipif(not _has("stajbot.collectors.ingest"), reason="ingest modülü henüz yok")
def test_real_ingest_import_csv(runner, cli_db, monkeypatch):
    monkeypatch.setitem(sys.modules, "stajbot.collectors.csv_import", None)  # yerel okuyucu
    r = runner.invoke(app, ["import-csv", str(TEMPLATE_CSV), "--source", "school"])
    assert r.exit_code == 0, r.output
    companies = _one(cli_db, Company)
    assert len(companies) == 2  # şehirsiz satır hedef dışı sayılır
    assert all(c.school_protocol for c in companies)
    r2 = runner.invoke(app, ["import-csv", str(TEMPLATE_CSV), "--source", "school"])
    assert r2.exit_code == 0 and len(_one(cli_db, Company)) == 2


@pytest.mark.skipif(not _has("stajbot.collectors.csv_import") or not _has("stajbot.collectors.ingest"), reason="csv_import/ingest modülü henüz yok")
def test_real_csv_import_module(runner, cli_db):
    r = runner.invoke(app, ["import-csv", str(TEMPLATE_CSV)])
    assert r.exit_code == 0, r.output
    assert len(_one(cli_db, Company)) >= 2


@pytest.mark.skipif(not _has("stajbot.mail.compose"), reason="compose modülü henüz yok")
def test_real_compose_no_llm(runner, cli_db):
    cid = _seed_ready_company(cli_db)
    r = runner.invoke(app, ["compose", "--company-id", str(cid), "--no-llm"])
    assert r.exit_code == 0, r.output
    msgs = _one(cli_db, EmailMessage, company_id=cid)
    assert len(msgs) == 1 and msgs[0].status == MailStatus.draft
    assert f"BV-{cid:04d}" in (msgs[0].subject or "")
    assert _one(cli_db, Company, id=cid)[0].status == AppStatus.drafted


@pytest.mark.skipif(not _has("stajbot.mail.sender"), reason="sender modülü henüz yok")
def test_real_send_dry_run(runner, cli_db):
    cid = _seed_ready_company(cli_db)
    Session = sessionmaker(bind=cli_db, expire_on_commit=False, future=True)
    with Session() as s:
        c = s.get(Company, cid)
        services.change_status(s, c, AppStatus.drafted, actor=Actor.system)
        s.add(EmailMessage(company_id=cid, direction=Direction.outbound, status=MailStatus.approved,
                           to_addr="kariyer@ornekyazilim.com", subject="Test", body_text="test", tracking_code=c.tracking_code))
        s.commit()
    r = runner.invoke(app, ["send", "--dry-run", "--force"])
    assert r.exit_code == 0, r.output
    assert "Prova (dry-run) bitti" in r.output
    # prova: gerçekte gönderilmiş sayılmaz
    msgs = _one(cli_db, EmailMessage, company_id=cid)
    assert all(m.status != MailStatus.sent for m in msgs)


@pytest.mark.skipif(not _has("stajbot.enrich.scoring"), reason="scoring modülü henüz yok")
def test_real_score(runner, cli_db):
    cid = _seed_ready_company(cli_db)
    r = runner.invoke(app, ["score"])
    assert r.exit_code == 0, r.output
    score = _one(cli_db, Company, id=cid)[0].score
    assert 0 <= score <= 100 and score > 0  # hedef şehir + HR e-postası en az +15
