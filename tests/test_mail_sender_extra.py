"""mail/sender.py için saldırgan testler: gün sınırı, limit sınırı, pencere sınırları, kural atlamaları,
MIME doğruluğu, SMTP/IMAP hata yolları, kesinti sonrası tutarlı durum, durum zinciri."""
from __future__ import annotations

import imaplib
import smtplib
from datetime import datetime, time as dtime, timedelta, timezone
from email import policy
from email.parser import BytesParser

import pytest

from stajbot import services
from stajbot.config import get_settings
from stajbot.enums import AppStatus, Direction, EventType, MailStatus
from stajbot.mail import sender
from stajbot.models import Company, EmailMessage
from stajbot.utils import now_utc

TR = timezone(timedelta(hours=3))


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mail_mode", "send")
    monkeypatch.setattr(s, "mail_user", "ben@gmail.com")
    monkeypatch.setattr(s, "mail_password", "app-pass")
    monkeypatch.setattr(s, "mail_from_name", "Şükrü Öğütçü")
    monkeypatch.setattr(s, "daily_send_limit", 15)
    monkeypatch.setattr(s, "send_delay_min_sec", 0)
    monkeypatch.setattr(s, "send_delay_max_sec", 0)
    monkeypatch.setattr(s, "attachments", [])
    monkeypatch.setattr(s, "smtp_port", 587)
    monkeypatch.setattr(s, "followup_days", 10)
    monkeypatch.setattr(s, "min_days_between_mails", 14)
    yield s


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.sent = []
        self.tls = False
        self.logged_in = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        self.tls = True

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def send_message(self, mime):
        self.sent.append(mime)


class FakeIMAP:
    appended: list = []
    list_rows: list = [b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Taslaklar"', b'(\\HasNoChildren) "/" "INBOX"']
    append_result = ("OK", [b"[APPENDUID 1 1]"])

    def __init__(self, host, port):
        pass

    def login(self, u, p):
        pass

    def list(self):
        return "OK", FakeIMAP.list_rows

    def append(self, folder, flags, date, data):
        FakeIMAP.appended.append((folder, flags, data))
        return FakeIMAP.append_result

    def logout(self):
        pass


def _approved(session, name="Alfa", domain="alfa.com", status=AppStatus.drafted, template=None, to=None, **kw) -> EmailMessage:
    c = Company(name=name, domain=domain, city="Konya", status=status)
    session.add(c)
    session.flush()
    services.add_contact(session, c, f"ik@{domain}", source="t")
    m = EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.approved, to_addr=to if to is not None else f"ik@{domain}",
                     subject=f"Staj Başvurusu — Şükrü [{c.tracking_code}]", body_text="Merhaba, İyi günler. ğüşıöç", tracking_code=c.tracking_code,
                     template_name=template, **kw)
    session.add(m)
    session.flush()
    return m


def _sent_msg(session, company, when, **kw) -> EmailMessage:
    m = EmailMessage(company=company, direction=Direction.outbound, status=MailStatus.sent, sent_at=when, to_addr="x@y.com", **kw)
    session.add(m)
    session.flush()
    return m


def _send(session, **kw):
    kw.setdefault("force", True)
    kw.setdefault("smtp_factory", FakeSMTP)
    kw.setdefault("sleeper", lambda s: None)
    return sender.send_approved(session, **kw)


# --- günlük sayaç: yerel takvim günü -----------------------------------------------

def test_sent_today_uses_local_calendar_day(session):
    c = Company(name="X", domain="x.com", city="Konya")
    session.add(c)
    session.flush()
    # 03.11 20:00 UTC = 03.11 23:00 TR (dün); 03.11 22:00 UTC = 04.11 01:00 TR (bugün)
    _sent_msg(session, c, datetime(2026, 11, 3, 20, 0))
    _sent_msg(session, c, datetime(2026, 11, 3, 22, 0))
    now = datetime(2026, 11, 3, 23, 30)  # 04.11 02:30 TR
    assert sender.sent_today(session, now, tz=TR) == 1
    assert sender.sent_today(session, now, tz=timezone.utc) == 2  # UTC gününde ikisi de "bugün"
    assert sender.sent_today(session, datetime(2026, 11, 4, 0, 1), tz=TR) == 1  # 04.11 03:01 TR: hâlâ aynı yerel gün


def test_local_day_start_utc():
    assert sender.local_day_start_utc(datetime(2026, 11, 3, 23, 30), TR) == datetime(2026, 11, 3, 21, 0)
    assert sender.local_day_start_utc(datetime(2026, 11, 3, 20, 59), TR) == datetime(2026, 11, 2, 21, 0)
    assert sender.local_day_start_utc(datetime(2026, 11, 3, 12, 0), timezone.utc) == datetime(2026, 11, 3, 0, 0)


def test_sent_today_ignores_failed_inbound_and_null_sent_at(session):
    c = Company(name="X", domain="x.com", city="Konya")
    session.add(c)
    session.flush()
    session.add(EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.failed, sent_at=now_utc(), to_addr="a@b.com"))
    session.add(EmailMessage(company=c, direction=Direction.inbound, status=MailStatus.received, sent_at=now_utc(), to_addr="a@b.com"))
    session.add(EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.sent, sent_at=None, to_addr="a@b.com"))
    session.flush()
    assert sender.sent_today(session) == 0


# --- limit sınırları -------------------------------------------------------------

def test_exactly_at_limit_sends_all_without_cut(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "daily_send_limit", 2)
    _approved(session, "A", "a.com")
    _approved(session, "B", "b.com")
    rep = _send(session)
    assert rep.sent == 2 and rep.skipped == 0 and rep.failed == 0
    assert not rep.reason or "limit" not in rep.reason


def test_one_below_limit_sends_exactly_one_more(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "daily_send_limit", 3)
    c = Company(name="Önce", domain="once.com", city="Konya", status=AppStatus.sent)
    session.add(c)
    session.flush()
    _sent_msg(session, c, now_utc() - timedelta(minutes=5))
    _sent_msg(session, c, now_utc() - timedelta(minutes=4))
    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = _send(session)
    assert rep.sent == 1 and rep.skipped == 1 and "limit" in rep.reason
    assert m1.status == MailStatus.sent and m2.status == MailStatus.approved


def test_force_does_not_bypass_daily_limit(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "daily_send_limit", 1)
    c = Company(name="Önce", domain="once.com", city="Konya", status=AppStatus.sent)
    session.add(c)
    session.flush()
    _sent_msg(session, c, now_utc() - timedelta(minutes=5))
    m = _approved(session)
    rep = _send(session, force=True)
    assert rep.sent == 0 and rep.skipped == 1 and "günlük limit doldu" in rep.reason and m.status == MailStatus.approved


def test_limit_param_smaller_than_remaining_and_reason_kept_in_dry_run(session):
    _approved(session, "A", "a.com")
    _approved(session, "B", "b.com")
    rep = _send(session, dry_run=True, limit=1)
    assert rep.sent == 1 and rep.skipped == 1
    assert "limit" in rep.reason and "prova" in rep.reason


# --- mesai penceresi sınırları ------------------------------------------------------

def test_window_boundaries_inclusive(_settings, monkeypatch):
    monkeypatch.setattr(_settings, "send_window", "09:00-17:30")
    monkeypatch.setattr(_settings, "send_weekdays_only", True)
    wed = datetime(2026, 11, 4)
    assert sender.within_send_window(wed.replace(hour=9, minute=0, second=0), _settings)
    assert not sender.within_send_window(wed.replace(hour=8, minute=59, second=59), _settings)
    assert sender.within_send_window(wed.replace(hour=17, minute=30, second=0), _settings)
    assert not sender.within_send_window(wed.replace(hour=17, minute=30, second=1), _settings)
    assert not sender.within_send_window(wed.replace(hour=0, minute=0), _settings)


def test_window_weekend_and_weekdays_only_flag(_settings, monkeypatch):
    monkeypatch.setattr(_settings, "send_weekdays_only", True)
    sat = datetime(2026, 11, 7, 10, 0)
    sun = datetime(2026, 11, 8, 10, 0)
    fri = datetime(2026, 11, 6, 10, 0)
    assert not sender.within_send_window(sat, _settings) and not sender.within_send_window(sun, _settings)
    assert sender.within_send_window(fri, _settings)
    monkeypatch.setattr(_settings, "send_weekdays_only", False)
    assert sender.within_send_window(sat, _settings) and sender.within_send_window(sun, _settings)


def test_window_custom_setting_is_respected(_settings, monkeypatch):
    monkeypatch.setattr(_settings, "send_window", "10:15-12:45")
    assert _settings.send_window_times == (dtime(10, 15), dtime(12, 45))
    assert sender.within_send_window(datetime(2026, 11, 4, 10, 15), _settings)
    assert not sender.within_send_window(datetime(2026, 11, 4, 10, 14), _settings)
    assert not sender.within_send_window(datetime(2026, 11, 4, 12, 46), _settings)


def test_outside_window_skip_does_not_touch_db_or_smtp(session, monkeypatch):
    FakeSMTP.instances.clear()
    m = _approved(session)
    monkeypatch.setattr(sender, "within_send_window", lambda now=None, settings=None: False)
    rep = sender.send_approved(session, smtp_factory=FakeSMTP, sleeper=lambda s: None)
    assert rep.sent == 0 and rep.skipped == 1 and "--force" in rep.reason
    assert m.status == MailStatus.approved and not FakeSMTP.instances


# --- dry_run / MAIL_MODE önceliği ----------------------------------------------------

def test_dry_run_wins_over_send_mode_even_without_credentials(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_password", "")
    FakeSMTP.instances.clear()
    m = _approved(session)
    rep = _send(session, dry_run=True)
    assert rep.sent == 1 and rep.dry_run and not FakeSMTP.instances
    assert m.status == MailStatus.approved and m.message_id is None and m.company.status == AppStatus.drafted


def test_dry_run_in_draft_mode_does_not_touch_imap(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "draft")
    FakeIMAP.appended.clear()
    m = _approved(session)
    rep = sender.send_approved(session, dry_run=True, force=True, imap_factory=FakeIMAP)
    assert rep.sent == 1 and rep.mode == "draft" and not FakeIMAP.appended and m.status == MailStatus.approved


def test_dry_run_still_applies_policy_skips(session):
    m = _approved(session, "Kara", "kara.com")
    m.company.blacklisted = True
    _approved(session, "Temiz", "temiz.com")
    rep = _send(session, dry_run=True)
    assert rep.sent == 1 and rep.skipped == 1 and any("kara listede" in e for e in rep.errors)


def test_dry_mode_does_not_sleep(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "dry")
    monkeypatch.setattr(_settings, "send_delay_min_sec", 5)
    monkeypatch.setattr(_settings, "send_delay_max_sec", 5)
    _approved(session, "A", "a.com")
    _approved(session, "B", "b.com")
    waits = []
    rep = sender.send_approved(session, force=True, sleeper=waits.append)
    assert rep.sent == 2 and waits == []


# --- kural atlamaları ------------------------------------------------------------------

@pytest.mark.parametrize("status", sorted({AppStatus.closed, AppStatus.accepted, AppStatus.negative}, key=lambda s: s.value))
def test_no_mail_statuses_skipped(session, status):
    m = _approved(session, status=status)
    rep = _send(session)
    assert rep.sent == 0 and rep.skipped == 1 and m.status == MailStatus.approved
    assert any(status.value in e for e in rep.errors)


def test_invalid_and_missing_recipient_skipped(session):
    m1 = _approved(session, "Bos", "bos.com", to="")
    m2 = _approved(session, "Bozuk", "bozuk.com", to="ik@")
    m3 = _approved(session, "Bosluk", "bosluk.com", to="ik @bosluk.com")
    rep = _send(session)
    assert rep.sent == 0 and rep.skipped == 3 and rep.failed == 0
    assert m1.status == m2.status == m3.status == MailStatus.approved
    assert any("alıcı adresi yok" in e for e in rep.errors) and sum("geçersiz" in e for e in rep.errors) == 2


def test_message_without_company_skipped(session):
    m = EmailMessage(company=None, direction=Direction.outbound, status=MailStatus.approved, to_addr="a@b.com", subject="x", body_text="y")
    session.add(m)
    session.flush()
    rep = _send(session)
    assert rep.sent == 0 and rep.skipped == 1 and any("şirket yok" in e for e in rep.errors)
    assert m.status == MailStatus.approved


def test_recent_mail_block_without_last_contact_at(session):
    """last_contact_at elle silinmiş olsa bile 14 gün kuralı gönderilmiş mail kaydına bakar."""
    m = _approved(session, status=AppStatus.sent)
    _sent_msg(session, m.company, now_utc() - timedelta(days=2))
    m.company.last_contact_at = None
    session.flush()
    rep = _send(session)
    assert rep.sent == 0 and rep.skipped == 1 and any("gün içinde" in e for e in rep.errors)


def test_min_days_boundary(session, _settings):
    m_old = _approved(session, "Eski", "eski.com", status=AppStatus.sent)
    _sent_msg(session, m_old.company, now_utc() - timedelta(days=14, minutes=5))
    m_new = _approved(session, "Yeni", "yeni.com", status=AppStatus.sent)
    _sent_msg(session, m_new.company, now_utc() - timedelta(days=13, hours=23))
    rep = _send(session)
    assert rep.sent == 1 and rep.skipped == 1
    assert m_old.status == MailStatus.sent and m_new.status == MailStatus.approved


def test_recent_block_ignores_failed_and_inbound(session):
    m = _approved(session, status=AppStatus.sent)
    session.add(EmailMessage(company=m.company, direction=Direction.outbound, status=MailStatus.failed, sent_at=now_utc() - timedelta(days=1), to_addr="a@b.com"))
    session.add(EmailMessage(company=m.company, direction=Direction.inbound, status=MailStatus.received, sent_at=now_utc() - timedelta(days=1), received_at=now_utc(), to_addr="a@b.com"))
    session.flush()
    rep = _send(session)
    assert rep.sent == 1


def test_followup_exempt_but_blacklist_still_applies(session):
    m = _approved(session, "Takip", "takip.com", status=AppStatus.no_reply, template="followup")
    _sent_msg(session, m.company, now_utc() - timedelta(days=3))
    m.company.blacklisted = True
    session.flush()
    rep = _send(session)
    assert rep.sent == 0 and rep.skipped == 1 and m.status == MailStatus.approved


def test_all_skipped_has_reason(session):
    m = _approved(session)
    m.company.blacklisted = True
    rep = _send(session)
    assert rep.sent == 0 and rep.reason and "gönderilebilir mesaj yok" in rep.reason


# --- MIME ----------------------------------------------------------------------------

def _roundtrip(mime):
    return BytesParser(policy=policy.default).parsebytes(mime.as_bytes())


def test_mime_utf8_subject_body_from_roundtrip(session, _settings):
    m = _approved(session)
    m.cc = "cc@alfa.com"
    mime = sender.build_mime(m, _settings)
    parsed = _roundtrip(mime)
    assert parsed["Subject"] == m.subject
    assert "Şükrü Öğütçü" in parsed["From"] and "ben@gmail.com" in parsed["From"]
    assert parsed["Cc"] == "cc@alfa.com"
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == m.body_text
    assert parsed["Message-ID"].endswith("@gmail.com>") and parsed["Message-ID"].startswith("<")
    assert "bv-0001" in parsed["Message-ID"]


def test_mime_no_duplicate_headers(session, _settings):
    m = _approved(session)
    m.message_id = "<zaten@var>"
    m.body_html = "<p>x</p>"
    mime = sender.build_mime(m, _settings, message_id="<yeni@gmail.com>")
    for h in ("From", "To", "Subject", "Message-ID", "Date", "MIME-Version", "Content-Type"):
        assert len(mime.get_all(h) or []) == 1, h
    assert mime["Message-ID"] == "<yeni@gmail.com>"


def test_mime_message_id_falls_back_without_mail_user(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_user", "")
    m = _approved(session)
    mid = sender.new_message_id(m, _settings)
    assert mid.endswith("@stajbot.local>")
    mime = sender.build_mime(m, _settings)
    assert "Şükrü Öğütçü" in mime["From"] and "@" not in mime["From"]


def test_mime_attachments_mimetype_and_dedupe(session, _settings, monkeypatch, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    docx = tmp_path / "transkript.docx"
    docx.write_bytes(b"PK\x03\x04")
    missing = tmp_path / "yok.pdf"
    monkeypatch.setattr(_settings, "attachments", [pdf, docx, pdf, missing])
    m = _approved(session)
    m.body_html = "<p>Merhaba</p>"
    mime = sender.build_mime(m, _settings)
    parts = list(mime.iter_attachments())
    assert [p.get_filename() for p in parts] == ["cv.pdf", "transkript.docx"]
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[1].get_content_type() == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert parts[0].get_payload(decode=True) == b"%PDF-1.4 x"
    body = mime.get_body(preferencelist=("html",))
    assert body is not None and "Merhaba" in body.get_content()
    assert mime.get_body(preferencelist=("plain",)).get_content().strip() == m.body_text


def test_mime_attachments_can_be_disabled(session, _settings, monkeypatch, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(_settings, "attachments", [pdf])
    m = _approved(session)
    assert list(sender.build_mime(m, _settings, attachments=False).iter_attachments()) == []


def test_sent_message_id_matches_header_and_is_unique(session):
    FakeSMTP.instances.clear()
    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = _send(session)
    assert rep.sent == 2
    smtp_sent = [mm for inst in FakeSMTP.instances for mm in inst.sent]
    assert {mm["Message-ID"] for mm in smtp_sent} == {m1.message_id, m2.message_id}
    assert m1.message_id != m2.message_id and m1.from_addr == "ben@gmail.com"


# --- SMTP hata yolları ------------------------------------------------------------------

def test_recipients_refused_marks_failed_and_continues(session):
    class Refuse(FakeSMTP):
        def send_message(self, mime):
            if "a.com" in mime["To"]:
                raise smtplib.SMTPRecipientsRefused({mime["To"]: (550, b"no such user")})
            super().send_message(mime)

    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = _send(session, smtp_factory=Refuse)
    assert rep.failed == 1 and rep.sent == 1 and rep.skipped == 0
    assert m1.status == MailStatus.failed and "SMTPRecipientsRefused" in m1.error and m1.sent_at is None
    assert m1.company.status == AppStatus.drafted  # şirket durumu değişmedi
    assert m2.status == MailStatus.sent


def test_connection_refused_stops_run_and_leaves_rest_approved(session):
    class Refused(FakeSMTP):
        def __init__(self, host, port, timeout=None):
            raise ConnectionRefusedError(111, "Connection refused")

    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    m3 = _approved(session, "C", "c.com")
    waits = []
    rep = _send(session, smtp_factory=Refused, sleeper=waits.append)
    assert rep.failed == 1 and rep.sent == 0 and rep.skipped == 2
    assert "bağlanılamadı" in rep.reason and waits == []
    assert m1.status == MailStatus.failed and "Bağlantı hatası" in m1.error
    assert m2.status == m3.status == MailStatus.approved


def test_dns_failure_stops_run(session):
    import socket

    class NoDns(FakeSMTP):
        def __init__(self, host, port, timeout=None):
            raise socket.gaierror(11001, "getaddrinfo failed")

    _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = _send(session, smtp_factory=NoDns)
    assert rep.failed == 1 and rep.skipped == 1 and m2.status == MailStatus.approved


def test_auth_failure_no_sleep_after_stop(session):
    class AuthFail(FakeSMTP):
        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    _approved(session, "A", "a.com")
    _approved(session, "B", "b.com")
    waits = []
    rep = _send(session, smtp_factory=AuthFail, sleeper=waits.append)
    assert rep.failed == 1 and rep.skipped == 1 and waits == []


def test_smtp_ssl_used_on_port_465(_settings, monkeypatch, session):
    monkeypatch.setattr(_settings, "smtp_port", 465)
    used = {}

    class SSL(FakeSMTP):
        def __init__(self, host, port, timeout=None):
            used["ssl"] = (host, port)
            super().__init__(host, port, timeout)

    class Plain(FakeSMTP):
        def __init__(self, host, port, timeout=None):
            used["plain"] = (host, port)
            super().__init__(host, port, timeout)

    monkeypatch.setattr(smtplib, "SMTP_SSL", SSL)
    monkeypatch.setattr(smtplib, "SMTP", Plain)
    m = _approved(session)
    mime = sender.build_mime(m, _settings)
    sender.smtp_send(mime, _settings)
    assert used == {"ssl": ("smtp.gmail.com", 465)}
    assert FakeSMTP.instances[-1].tls is False  # 465'te STARTTLS yok


def test_smtp_starttls_on_587(_settings, monkeypatch, session):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    m = _approved(session)
    sender.smtp_send(sender.build_mime(m, _settings), _settings)
    assert FakeSMTP.instances[-1].tls is True and FakeSMTP.instances[-1].logged_in == ("ben@gmail.com", "app-pass")


def test_build_mime_error_marks_failed_not_crash(session, monkeypatch):
    def boom(*a, **k):
        raise ValueError("şablon bozuk")

    monkeypatch.setattr(sender, "build_mime", boom)
    m = _approved(session)
    rep = _send(session)
    assert rep.failed == 1 and m.status == MailStatus.failed and "ValueError" in m.error


# --- bekleme ------------------------------------------------------------------------------

def test_sleep_between_only_not_after_last_and_after_failed(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "send_delay_min_sec", 3)
    monkeypatch.setattr(_settings, "send_delay_max_sec", 7)

    class Flaky(FakeSMTP):
        def send_message(self, mime):
            if "b.com" in mime["To"]:
                raise OSError("koptu")
            super().send_message(mime)

    _approved(session, "A", "a.com")
    _approved(session, "B", "b.com")
    _approved(session, "C", "c.com")
    waits = []
    rep = _send(session, smtp_factory=Flaky, sleeper=waits.append)
    assert rep.sent == 2 and rep.failed == 1
    assert len(waits) == 2 and all(3 <= w <= 7 for w in waits)


def test_single_message_no_sleep(session):
    _approved(session)
    waits = []
    _send(session, sleeper=waits.append)
    assert waits == []


# --- durum / Event / takip -------------------------------------------------------------

def test_followup_dates_and_events(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "followup_days", 7)
    m = _approved(session)
    rep = _send(session)
    assert rep.sent_ids == [m.id]
    c = m.company
    assert c.next_followup_at == m.sent_at + timedelta(days=7) and c.last_contact_at == m.sent_at
    types = [(e.type, e.old_status, e.new_status) for e in c.events]
    assert (EventType.status_change, AppStatus.drafted, AppStatus.queued) in types
    assert (EventType.status_change, AppStatus.queued, AppStatus.sent) in types
    sent_ev = [e for e in c.events if e.type == EventType.email_sent]
    assert len(sent_ev) == 1 and sent_ev[0].email_message_id == m.id and "SMTP" in sent_ev[0].body and sent_ev[0].occurred_at == m.sent_at


def test_company_already_queued_goes_to_sent(session):
    m = _approved(session, status=AppStatus.queued)
    _send(session)
    assert m.company.status == AppStatus.sent
    assert sum(1 for e in m.company.events if e.type == EventType.status_change) == 1


def test_company_ready_chains_to_sent(session):
    m = _approved(session, status=AppStatus.ready)
    _send(session)
    assert m.company.status == AppStatus.sent


@pytest.mark.parametrize("status", [AppStatus.replied, AppStatus.positive, AppStatus.interview, AppStatus.found])
def test_other_statuses_untouched_but_mail_sent(session, status):
    m = _approved(session, status=status)
    rep = _send(session)
    assert rep.sent == 1 and m.status == MailStatus.sent and m.company.status == status
    assert not any(e.type == EventType.status_change for e in m.company.events)


def test_followup_from_sent_status_goes_to_followup_sent(session):
    m = _approved(session, "Takip", "takip.com", status=AppStatus.sent, template="followup")
    _sent_msg(session, m.company, now_utc() - timedelta(days=11))
    rep = _send(session)
    assert rep.sent == 1 and m.company.status == AppStatus.followup_sent


def test_followup_in_other_status_keeps_status(session):
    m = _approved(session, "Takip", "takip.com", status=AppStatus.replied, template="followup")
    rep = _send(session)
    assert rep.sent == 1 and m.company.status == AppStatus.replied


def test_failed_message_keeps_company_and_followup_untouched(session):
    class Fail(FakeSMTP):
        def send_message(self, mime):
            raise OSError("x")

    m = _approved(session, status=AppStatus.queued)
    _send(session, smtp_factory=Fail)
    c = m.company
    assert m.status == MailStatus.failed and c.status == AppStatus.queued and c.next_followup_at is None and c.last_contact_at is None
    assert not any(e.type == EventType.email_sent for e in c.events)


# --- kesinti: tutarlı durum ------------------------------------------------------------------

def test_each_message_committed_so_rollback_keeps_sent(session):
    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = _send(session)
    assert rep.sent == 2
    session.rollback()  # çağıran commit etmeden ölse bile...
    session.refresh(m1)
    session.refresh(m2)
    assert m1.status == MailStatus.sent and m2.status == MailStatus.sent
    session.refresh(m1.company)
    assert m1.company.status == AppStatus.sent


def test_ctrl_c_during_sleep_keeps_sent_and_leaves_rest_approved(session):
    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    m3 = _approved(session, "C", "c.com")

    def interrupt(_):
        raise KeyboardInterrupt

    rep = _send(session, sleeper=interrupt)
    assert rep.sent == 1 and rep.skipped == 2 and rep.failed == 0 and "durdurdu" in rep.reason
    session.rollback()
    for m in (m1, m2, m3):
        session.refresh(m)
    assert m1.status == MailStatus.sent and m2.status == MailStatus.approved and m3.status == MailStatus.approved


def test_ctrl_c_during_send_marks_failed_not_resent(session):
    class Interrupt(FakeSMTP):
        def send_message(self, mime):
            raise KeyboardInterrupt

    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = _send(session, smtp_factory=Interrupt)
    assert rep.failed == 1 and rep.skipped == 1 and rep.sent == 0
    session.rollback()
    session.refresh(m1)
    session.refresh(m2)
    assert m1.status == MailStatus.failed and "kesildi" in m1.error and m2.status == MailStatus.approved
    # ikinci çalıştırma m1'i yeniden GÖNDERMEZ
    rep2 = _send(session)
    assert rep2.sent == 1 and m1.status == MailStatus.failed


def test_failed_message_is_committed(session):
    class Fail(FakeSMTP):
        def send_message(self, mime):
            raise OSError("x")

    m = _approved(session)
    _send(session, smtp_factory=Fail)
    session.rollback()
    session.refresh(m)
    assert m.status == MailStatus.failed


# --- Gmail taslak modu -------------------------------------------------------------------------

def test_draft_mode_uses_localized_drafts_folder(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "draft")
    FakeIMAP.appended.clear()
    monkeypatch.setattr(FakeIMAP, "list_rows", [b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Taslaklar"', b'(\\HasNoChildren) "/" "INBOX"'])
    m = _approved(session)
    rep = sender.send_approved(session, force=True, imap_factory=FakeIMAP)
    assert rep.sent == 1 and m.status == MailStatus.sent and m.sent_at is not None
    folder, flags, data = FakeIMAP.appended[0]
    assert folder == '"[Gmail]/Taslaklar"' and "\\Draft" in flags
    parsed = BytesParser(policy=policy.default).parsebytes(data)
    assert parsed["Message-ID"] == m.message_id and parsed["To"] == m.to_addr
    ev = [e for e in m.company.events if e.type == EventType.email_sent][0]
    assert "Taslaklar" in ev.body and "Gmail" in ev.body
    assert m.company.status == AppStatus.sent


def test_draft_mode_falls_back_to_default_folder(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "draft")
    FakeIMAP.appended.clear()
    monkeypatch.setattr(FakeIMAP, "list_rows", [b'(\\HasNoChildren) "/" "INBOX"'])
    _approved(session)
    sender.send_approved(session, force=True, imap_factory=FakeIMAP)
    assert FakeIMAP.appended[0][0] == f'"{sender.GMAIL_DRAFTS}"'


def test_find_drafts_folder_handles_missing_list_and_odd_rows():
    class NoList:
        pass

    class Odd:
        def list(self):
            return "OK", [None, b"garbage", b'(\\Drafts \\HasNoChildren) "/" Taslaklar']

    class Fails:
        def list(self):
            raise imaplib.IMAP4.error("x")

    assert sender.find_drafts_folder(NoList()) == sender.GMAIL_DRAFTS
    assert sender.find_drafts_folder(Odd()) == "Taslaklar"
    assert sender.find_drafts_folder(Fails()) == sender.GMAIL_DRAFTS


def test_draft_mode_append_no_marks_failed_and_continues(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "draft")
    FakeIMAP.appended.clear()

    class NoAppend(FakeIMAP):
        n = 0

        def append(self, folder, flags, date, data):
            NoAppend.n += 1
            if NoAppend.n == 1:
                return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
            return super().append(folder, flags, date, data)

    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = sender.send_approved(session, force=True, imap_factory=NoAppend, sleeper=lambda s: None)
    assert rep.failed == 1 and rep.sent == 1
    assert m1.status == MailStatus.failed and "APPEND" in m1.error and "NONEXISTENT" in m1.error
    assert m2.status == MailStatus.sent


def test_draft_mode_login_error_stops(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "draft")

    class BadLogin(FakeIMAP):
        def login(self, u, p):
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

    m1 = _approved(session, "A", "a.com")
    m2 = _approved(session, "B", "b.com")
    rep = sender.send_approved(session, force=True, imap_factory=BadLogin, sleeper=lambda s: None)
    assert rep.failed == 1 and rep.skipped == 1 and rep.sent == 0
    assert m1.status == MailStatus.failed and "IMAP" in m1.error and m2.status == MailStatus.approved
    assert "uygulama şifresi" in rep.reason


def test_draft_mode_logout_called_even_on_error(_settings, monkeypatch, session):
    monkeypatch.setattr(_settings, "mail_mode", "draft")
    calls = []

    class Track(FakeIMAP):
        def append(self, *a):
            raise RuntimeError("x")

        def logout(self):
            calls.append("logout")

    _approved(session)
    sender.send_approved(session, force=True, imap_factory=Track)
    assert calls == ["logout"]


def test_draft_mode_requires_credentials(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "draft")
    monkeypatch.setattr(_settings, "mail_user", "")
    _approved(session)
    rep = sender.send_approved(session, force=True, imap_factory=FakeIMAP)
    assert rep.sent == 0 and "MAIL_USER" in rep.reason


# --- rapor / sıralama / çeşitli ---------------------------------------------------------------

def test_send_order_is_created_at_then_id(session):
    FakeSMTP.instances.clear()
    m_late = _approved(session, "Gec", "gec.com")
    m_late.created_at = now_utc() + timedelta(hours=1)
    m_early = _approved(session, "Erken", "erken.com")
    m_early.created_at = now_utc() - timedelta(hours=1)
    session.flush()
    rep = _send(session)
    assert rep.sent_ids == [m_early.id, m_late.id]


def test_only_outbound_approved_selected(session):
    c = Company(name="X", domain="x.com", city="Konya")
    session.add(c)
    session.flush()
    session.add(EmailMessage(company=c, direction=Direction.inbound, status=MailStatus.approved, to_addr="a@b.com"))
    session.add(EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.draft, to_addr="a@b.com"))
    session.add(EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.queued, to_addr="a@b.com"))
    session.flush()
    rep = _send(session)
    assert rep.reason == "onaylı mesaj yok" and rep.sent == 0


def test_summary_turkish_and_contains_counts(session):
    rep = sender.SendReport(sent=2, skipped=1, failed=0, mode="send", reason="x")
    s = rep.summary()
    assert "Gönderim" in s and "gönderilen: 2" in s and "atlanan: 1" in s and "(x)" in s
    assert "Prova" in sender.SendReport(dry_run=True).summary()


def test_module_hygiene():
    import inspect

    src = inspect.getsource(sender)
    assert "print(" not in src
    assert "utcnow" not in src
