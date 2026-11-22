"""mail/sender.py testleri: prova, SMTP (sahte), Gmail taslağı (sahte), limitler, pencere, kurallar."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from stajbot import services
from stajbot.config import get_settings
from stajbot.enums import Actor, AppStatus, Direction, EventType, MailStatus
from stajbot.mail import sender
from stajbot.models import Company, EmailMessage
from stajbot.utils import now_utc


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mail_mode", "send")
    monkeypatch.setattr(s, "mail_user", "ben@gmail.com")
    monkeypatch.setattr(s, "mail_password", "app-pass")
    monkeypatch.setattr(s, "mail_from_name", "Ad Soyad")
    monkeypatch.setattr(s, "daily_send_limit", 15)
    monkeypatch.setattr(s, "send_delay_min_sec", 0)
    monkeypatch.setattr(s, "send_delay_max_sec", 0)
    monkeypatch.setattr(s, "attachments", [])
    yield s


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.sent = []
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


class AuthFailSMTP(FakeSMTP):
    def login(self, user, pw):
        import smtplib

        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")


class FakeIMAP:
    appended = []

    def __init__(self, host, port):
        pass

    def login(self, u, p):
        pass

    def append(self, folder, flags, date, data):
        FakeIMAP.appended.append((folder, data))
        return "OK", [b"[APPENDUID 1 1]"]

    def logout(self):
        pass


def _approved(session, name="Alfa", domain="alfa.com", status=AppStatus.drafted, template=None, **kw) -> EmailMessage:
    c = Company(name=name, domain=domain, city="Konya", status=status)
    session.add(c)
    session.flush()
    services.add_contact(session, c, f"ik@{domain}", source="t")
    m = EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.approved, to_addr=f"ik@{domain}",
                     subject=f"Staj [{c.tracking_code}]", body_text="merhaba", tracking_code=c.tracking_code, template_name=template, **kw)
    session.add(m)
    session.flush()
    return m


def test_dry_run_changes_nothing(session):
    m = _approved(session)
    rep = sender.send_approved(session, dry_run=True, force=True)
    assert rep.sent == 1 and rep.dry_run and "prova" in rep.reason
    assert m.status == MailStatus.approved and m.company.status == AppStatus.drafted and m.sent_at is None


def test_mode_dry_only_logs(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "dry")
    m = _approved(session)
    rep = sender.send_approved(session, force=True)
    assert rep.sent == 1 and rep.mode == "dry" and m.status == MailStatus.approved


def test_smtp_send_marks_sent_and_transitions(session):
    FakeSMTP.instances.clear()
    m = _approved(session)
    rep = sender.send_approved(session, force=True, smtp_factory=FakeSMTP, sleeper=lambda s: None)
    assert rep.sent == 1 and rep.failed == 0, rep
    assert m.status == MailStatus.sent and m.sent_at is not None and "bv-0001@" in m.message_id
    c = m.company
    assert c.status == AppStatus.sent and c.last_contact_at and c.next_followup_at > c.last_contact_at
    assert any(e.type == EventType.email_sent for e in c.events)
    smtp = FakeSMTP.instances[0]
    assert smtp.logged_in == ("ben@gmail.com", "app-pass") and smtp.tls
    mime = smtp.sent[0]
    assert mime["To"] == "ik@alfa.com" and mime["Message-ID"] == m.message_id and "Ad Soyad" in mime["From"]
    assert mime.get_content().strip() == "merhaba"


def test_draft_mode_appends_to_gmail(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_mode", "draft")
    FakeIMAP.appended.clear()
    m = _approved(session)
    rep = sender.send_approved(session, force=True, imap_factory=FakeIMAP)
    assert rep.sent == 1 and m.status == MailStatus.sent
    assert FakeIMAP.appended and "Drafts" in FakeIMAP.appended[0][0]
    ev = [e for e in m.company.events if e.type == EventType.email_sent][0]
    assert "Gmail" in (ev.body or "")


def test_outside_window_skips_unless_force(session, _settings, monkeypatch):
    _approved(session)
    monkeypatch.setattr(sender, "within_send_window", lambda now=None, settings=None: False)
    rep = sender.send_approved(session, smtp_factory=FakeSMTP)
    assert rep.sent == 0 and rep.skipped == 1 and "mesai" in rep.reason
    rep2 = sender.send_approved(session, force=True, smtp_factory=FakeSMTP, sleeper=lambda s: None)
    assert rep2.sent == 1


def test_within_send_window():
    s = get_settings()
    assert sender.within_send_window(datetime(2026, 11, 4, 10, 0), s)  # Çarşamba
    assert not sender.within_send_window(datetime(2026, 11, 4, 18, 0), s)
    assert not sender.within_send_window(datetime(2026, 11, 7, 10, 0), s)  # Cumartesi


def test_daily_limit_and_limit_param(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "daily_send_limit", 2)
    for i in range(3):
        _approved(session, name=f"F{i}", domain=f"f{i}.com")
    rep = sender.send_approved(session, force=True, smtp_factory=FakeSMTP, sleeper=lambda s: None)
    assert rep.sent == 2 and rep.skipped == 1 and "limit" in rep.reason
    rep2 = sender.send_approved(session, force=True, smtp_factory=FakeSMTP)
    assert rep2.sent == 0 and "günlük limit doldu" in rep2.reason
    monkeypatch.setattr(_settings, "daily_send_limit", 10)
    rep3 = sender.send_approved(session, force=True, limit=1, smtp_factory=FakeSMTP)
    assert rep3.sent == 1


def test_skips_blacklisted_closed_and_recent(session):
    m1 = _approved(session, name="Kara", domain="kara.com")
    m1.company.blacklisted = True
    m2 = _approved(session, name="Kapali", domain="kapali.com", status=AppStatus.closed)
    m3 = _approved(session, name="Yakin", domain="yakin.com", status=AppStatus.sent)
    m3.company.last_contact_at = now_utc() - timedelta(days=3)
    session.add(EmailMessage(company=m3.company, direction=Direction.outbound, status=MailStatus.sent, sent_at=now_utc() - timedelta(days=3), to_addr="x@yakin.com"))
    m4 = _approved(session, name="Eski", domain="eski.com", status=AppStatus.sent)
    m4.company.last_contact_at = now_utc() - timedelta(days=30)
    session.add(EmailMessage(company=m4.company, direction=Direction.outbound, status=MailStatus.sent, sent_at=now_utc() - timedelta(days=30), to_addr="x@eski.com"))
    session.flush()
    rep = sender.send_approved(session, force=True, smtp_factory=FakeSMTP, sleeper=lambda s: None)
    assert rep.sent == 1 and rep.skipped == 3
    assert m4.status == MailStatus.sent and m1.status == m2.status == m3.status == MailStatus.approved
    assert any("kara listede" in e for e in rep.errors) and any("gün içinde" in e for e in rep.errors)


def test_followup_exempt_from_min_days_and_transitions(session):
    m = _approved(session, name="Takip", domain="takip.com", status=AppStatus.no_reply, template="followup")
    c = m.company
    c.last_contact_at = now_utc() - timedelta(days=11)
    session.add(EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.sent, sent_at=now_utc() - timedelta(days=11), to_addr="ik@takip.com", message_id="<ilk@x>"))
    m.in_reply_to = "<ilk@x>"
    session.flush()
    rep = sender.send_approved(session, force=True, smtp_factory=FakeSMTP, sleeper=lambda s: None)
    assert rep.sent == 1 and c.status == AppStatus.followup_sent
    assert FakeSMTP.instances[-1].sent[0]["In-Reply-To"] == "<ilk@x>"


def test_same_company_twice_in_one_run(session):
    m = _approved(session)
    m2 = EmailMessage(company=m.company, direction=Direction.outbound, status=MailStatus.approved, to_addr=m.to_addr, subject="2", body_text="2")
    session.add(m2)
    session.flush()
    rep = sender.send_approved(session, force=True, smtp_factory=FakeSMTP, sleeper=lambda s: None)
    assert rep.sent == 1 and rep.skipped == 1 and m2.status == MailStatus.approved


def test_auth_failure_marks_failed_and_stops(session):
    m1 = _approved(session, name="A", domain="a.com")
    m2 = _approved(session, name="B", domain="b.com")
    rep = sender.send_approved(session, force=True, smtp_factory=AuthFailSMTP, sleeper=lambda s: None)
    assert rep.failed == 1 and rep.sent == 0 and rep.skipped == 1 and "kimlik" in rep.reason.lower()
    assert m1.status == MailStatus.failed and "Kimlik" in m1.error and m2.status == MailStatus.approved


def test_generic_failure_continues(session):
    class Flaky(FakeSMTP):
        n = 0

        def send_message(self, mime):
            Flaky.n += 1
            if Flaky.n == 1:
                raise OSError("bağlantı koptu")
            super().send_message(mime)

    _approved(session, name="A", domain="a.com")
    m2 = _approved(session, name="B", domain="b.com")
    rep = sender.send_approved(session, force=True, smtp_factory=Flaky, sleeper=lambda s: None)
    assert rep.failed == 1 and rep.sent == 1 and m2.status == MailStatus.sent


def test_no_credentials_in_send_mode(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_password", "")
    _approved(session)
    rep = sender.send_approved(session, force=True)
    assert rep.sent == 0 and "MAIL_USER" in rep.reason


def test_nothing_to_send(session):
    rep = sender.send_approved(session, force=True)
    assert rep.sent == 0 and rep.reason == "onaylı mesaj yok"


def test_build_mime_with_attachment(session, _settings, monkeypatch, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(_settings, "attachments", [pdf])
    m = _approved(session)
    m.body_html = "<p>merhaba</p>"
    mime = sender.build_mime(m, _settings)
    parts = list(mime.iter_attachments())
    assert len(parts) == 1 and parts[0].get_filename() == "cv.pdf" and parts[0].get_content_type() == "application/pdf"
    assert mime.get_body(preferencelist=("html",)) is not None


def test_sleeper_called_between_messages(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "send_delay_min_sec", 5)
    monkeypatch.setattr(_settings, "send_delay_max_sec", 5)
    _approved(session, name="A", domain="a.com")
    _approved(session, name="B", domain="b.com")
    waits = []
    sender.send_approved(session, force=True, smtp_factory=FakeSMTP, sleeper=waits.append)
    assert waits == [5]


def test_no_print():
    import inspect

    assert "print(" not in inspect.getsource(sender)
