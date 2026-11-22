"""mail/inbox.py ve mail/matcher.py testleri (sahte IMAP)."""
from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage as Mime

import pytest
from sqlalchemy import select

from stajbot import services
from stajbot.config import get_settings
from stajbot.db import get_setting, set_setting
from stajbot.enums import AppStatus, Direction, EventType, MailStatus, MatchMethod, Sentiment
from stajbot.mail import inbox, matcher
from stajbot.models import Company, EmailMessage


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mail_user", "ben@gmail.com")
    monkeypatch.setattr(s, "mail_password", "pw")
    yield s


def _mime(frm="ik@alfa.com", to="ben@gmail.com", subject="Re: Staj [BV-0001]", body="Merhaba", *, in_reply_to=None, msgid="<r1@alfa.com>", html=None, date="Tue, 03 Nov 2026 10:00:00 +0300") -> bytes:
    m = Mime()
    m["From"] = f"İK <{frm}>"
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = msgid
    m["Date"] = date
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    if html:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


def _sent_company(session, name="Alfa", domain="alfa.com", msgid="<out1@stajbot.local>") -> Company:
    c = Company(name=name, domain=domain, city="Konya", status=AppStatus.sent)
    session.add(c)
    session.flush()
    services.add_contact(session, c, f"ik@{domain}", source="t")
    session.add(EmailMessage(company=c, direction=Direction.outbound, status=MailStatus.sent, message_id=msgid, to_addr=f"ik@{domain}", subject=f"Staj [{c.tracking_code}]", sent_at=datetime(2026, 11, 1)))
    session.flush()
    return c


# --- parse ------------------------------------------------------------------------

def test_parse_message_plain_and_html():
    msg = inbox.parse_message(_mime(body="Merhaba,\nmülakata davet ediyoruz."), uid=7)
    assert msg.direction == Direction.inbound and msg.status == MailStatus.received
    assert msg.from_addr == "ik@alfa.com" and msg.subject.startswith("Re:") and msg.imap_uid == 7
    assert msg.tracking_code == "BV-0001" and msg.received_at == datetime(2026, 11, 3, 7, 0)
    h = inbox.parse_message(_mime(html="<html><body><p>Merhaba&nbsp;dünya</p><p>Görüşelim</p><script>x</script></body></html>"))
    assert "Merhaba" in h.body_text and "Görüşelim" in h.body_text and "script" not in h.body_text and h.body_html


def test_parse_bad_date_and_missing_fields():
    raw = b"Subject: test\r\n\r\nbody"
    msg = inbox.parse_message(raw)
    assert msg.from_addr is None and msg.received_at is not None and msg.body_text == "body"


# --- matcher ------------------------------------------------------------------------

def test_guess_sentiment():
    assert matcher.guess_sentiment("Maalesef bu dönem kontenjanımız dolmuştur. Başarılar dileriz.") == Sentiment.negative
    assert matcher.guess_sentiment("Merhaba, sizi mülakata davet ediyoruz; hangi gün müsaitsiniz?") == Sentiment.positive
    assert matcher.guess_sentiment("Transkriptinizi ve okulun protokol belgesini iletebilir misiniz?") == Sentiment.info
    assert matcher.guess_sentiment("Teşekkürler.") == Sentiment.unknown
    assert matcher.guess_sentiment("") == Sentiment.unknown


def test_reply_body_strips_quote():
    text = "Olumlu, görüşelim.\n\nOn Mon, X wrote:\n> eski mesaj maalesef"
    assert matcher.reply_body(text) == "Olumlu, görüşelim."
    assert matcher.guess_sentiment(text) == Sentiment.positive


def test_match_by_message_id(session):
    c = _sent_company(session)
    msg = inbox.parse_message(_mime(subject="Re: hakkında", in_reply_to="<out1@stajbot.local>"))
    session.add(msg)
    session.flush()
    res = matcher.match(msg, session)
    assert res.company is c and res.method == MatchMethod.message_id
    assert msg.company_id == c.id and msg.matched_by == MatchMethod.message_id and msg.tracking_code == c.tracking_code
    assert c.status == AppStatus.replied and c.last_contact_at == msg.received_at
    assert any(e.type == EventType.email_received for e in c.events)


def test_match_by_tracking_code_and_domain(session):
    c = _sent_company(session)
    m1 = inbox.parse_message(_mime(frm="baska@gmail.com", subject=f"Yanıt [{c.tracking_code}]", msgid="<a@x>"))
    session.add(m1)
    session.flush()
    assert matcher.match(m1, session).method == MatchMethod.tracking_code
    m2 = inbox.parse_message(_mime(frm="ayse.y@alfa.com", subject="Staj hk.", msgid="<b@x>"))
    session.add(m2)
    session.flush()
    assert matcher.match(m2, session).method == MatchMethod.domain
    # kişi alan adı üzerinden (Company.domain farklı)
    c2 = Company(name="Beta", domain="beta-holding.com", city="Konya", status=AppStatus.sent)
    session.add(c2)
    session.flush()
    services.add_contact(session, c2, "hr@beta.com.tr", source="t")
    m3 = inbox.parse_message(_mime(frm="kisi@beta.com.tr", subject="x", msgid="<c@x>"))
    session.add(m3)
    session.flush()
    assert matcher.match(m3, session).company is c2


def test_unmatched_generic_domain(session):
    _sent_company(session)
    m = inbox.parse_message(_mime(frm="x@gmail.com", subject="reklam", msgid="<z@x>"))
    session.add(m)
    session.flush()
    res = matcher.match(m, session)
    assert res.company is None and res.method == MatchMethod.none and m.matched_by == MatchMethod.none


def test_match_does_not_change_non_sent_status(session):
    c = _sent_company(session)
    c.status = AppStatus.interview
    m = inbox.parse_message(_mime(in_reply_to="<out1@stajbot.local>"))
    session.add(m)
    session.flush()
    matcher.match(m, session)
    assert c.status == AppStatus.interview and m.company_id == c.id


# --- fetch_new with fake IMAP -------------------------------------------------------

class FakeIMAP:
    mailbox: dict[int, bytes] = {}
    fail_login = False

    def __init__(self, host, port):
        self.host = host

    def login(self, u, p):
        if FakeIMAP.fail_login:
            raise Exception("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        if cmd == "search":
            spec = args[1]
            start = int(spec.split()[1].split(":")[0])
            ids = [str(u).encode() for u in sorted(FakeIMAP.mailbox) if u >= start]
            return "OK", [b" ".join(ids)]
        if cmd == "fetch":
            uid = int(args[0])
            raw = FakeIMAP.mailbox.get(uid)
            if raw is None:
                return "NO", [None]
            return "OK", [(f"{uid} (RFC822 {{{len(raw)}}}".encode(), raw), b")"]
        raise AssertionError(cmd)

    def logout(self):
        pass


def test_fetch_new_stores_matches_and_tracks_uid(session):
    c = _sent_company(session)
    FakeIMAP.fail_login = False
    FakeIMAP.mailbox = {
        1: _mime(in_reply_to="<out1@stajbot.local>", msgid="<m1@x>", body="Görüşme için uygun musunuz?"),
        2: _mime(frm="spam@gmail.com", subject="indirim", msgid="<m2@x>"),
        3: _mime(frm="ben@gmail.com", subject="kendi mailim", msgid="<m3@x>"),
    }
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert len(new) == 2
    assert new[0].company_id == c.id and new[0].sentiment == Sentiment.positive and new[1].company_id is None
    assert get_setting(session, inbox.LAST_UID_KEY) == "3"
    assert c.status == AppStatus.replied
    # ikinci çalıştırma yeni mesaj getirmez
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    FakeIMAP.mailbox[4] = _mime(msgid="<m1@x>", in_reply_to="<out1@stajbot.local>")  # aynı Message-ID -> tekil
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    assert get_setting(session, inbox.LAST_UID_KEY) == "4"
    assert len(session.scalars(select(EmailMessage).where(EmailMessage.direction == Direction.inbound)).all()) == 2


def test_fetch_new_handles_login_failure(session):
    FakeIMAP.fail_login = True
    try:
        assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    finally:
        FakeIMAP.fail_login = False


def test_fetch_new_without_credentials(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_password", "")
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []


def test_fetch_new_broken_message_does_not_abort(session):
    FakeIMAP.fail_login = False
    set_setting(session, inbox.LAST_UID_KEY, "0")
    FakeIMAP.mailbox = {1: b"\xff\xfe garbage", 2: _mime(frm="a@b.com", subject="ok", msgid="<ok@x>")}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert len(new) >= 1 and get_setting(session, inbox.LAST_UID_KEY) == "2"


def test_no_print():
    import inspect

    for mod in (inbox, matcher):
        assert "print(" not in inspect.getsource(mod)
