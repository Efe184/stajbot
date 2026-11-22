"""mail/inbox.py ve mail/matcher.py için saldırgan (adversarial) testler.

Ağa çıkılmaz; IMAP sahte sınıfla taklit edilir. Odak: UID aralığı uç durumları, karakter kodlamaları,
HTML alıntılar, eşleştirme sırası, alt alan adı / Reply-To eşleşmesi, durum geçişleri, duygu tahmini.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
from email.message import EmailMessage as Mime

import pytest
from sqlalchemy import select

from stajbot import services
from stajbot.config import get_settings
from stajbot.db import get_setting, set_setting
from stajbot.enums import AppStatus, Direction, EventType, MailStatus, MatchMethod, Sentiment
from stajbot.mail import inbox, matcher
from stajbot.models import Company, EmailMessage
from stajbot.utils import now_utc


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mail_user", "ben@gmail.com")
    monkeypatch.setattr(s, "mail_password", "pw")
    yield s


def _mime(
    frm="ik@alfa.com",
    to="ben@gmail.com",
    subject="Re: Staj",
    body="Merhaba",
    *,
    in_reply_to=None,
    references=None,
    msgid="<r1@alfa.com>",
    html=None,
    date="Tue, 03 Nov 2026 10:00:00 +0300",
    headers: dict | None = None,
) -> bytes:
    m = Mime()
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    if msgid:
        m["Message-ID"] = msgid
    if date:
        m["Date"] = date
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = references
    for k, v in (headers or {}).items():
        m[k] = v
    if html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


def _company(session, name="Alfa", domain="alfa.com", status=AppStatus.sent, msgid="<out1@stajbot.local>", contact=None) -> Company:
    c = Company(name=name, domain=domain, city="Konya", status=status)
    session.add(c)
    session.flush()
    services.add_contact(session, c, contact or f"ik@{domain}", source="t")
    if msgid:
        session.add(
            EmailMessage(
                company=c, direction=Direction.outbound, status=MailStatus.sent, message_id=msgid,
                to_addr=f"ik@{domain}", subject=f"Staj [{c.tracking_code}]", sent_at=datetime(2026, 11, 1),
            )
        )
    session.flush()
    return c


def _stored(session, raw: bytes, uid: int | None = None) -> EmailMessage:
    msg = inbox.parse_message(raw, uid)
    session.add(msg)
    session.flush()
    return msg


# --- ayrıştırma: adresler, kodlamalar, HTML -----------------------------------------------

def test_from_with_unquoted_comma_display_name_is_parsed():
    raw = b"From: IK Birimi, Alfa A.S. <ik@alfa.com>\r\nTo: ben@gmail.com\r\nSubject: x\r\n\r\nbody\r\n"
    msg = inbox.parse_message(raw)
    assert msg.from_addr == "ik@alfa.com"


def test_from_with_turkish_display_name_and_uppercase_address():
    msg = inbox.parse_message(_mime(frm="Ayşe Yılmaz <Ayse.Yilmaz@Alfa.COM>"))
    assert msg.from_addr == "ayse.yilmaz@alfa.com"


def test_to_with_multiple_recipients_keeps_first():
    raw = b"From: ik@alfa.com\r\nTo: ben@gmail.com, baska@x.com\r\nSubject: x\r\n\r\nbody\r\n"
    msg = inbox.parse_message(raw)
    assert msg.to_addr == "ben@gmail.com"


def test_unknown_charset_body_is_not_lost():
    raw = b"From: ik@alfa.com\r\nSubject: x\r\nContent-Type: text/plain; charset=x-bilinmeyen\r\n\r\nMaalesef kontenjan yok\r\n"
    msg = inbox.parse_message(raw)
    assert msg.body_text and "Maalesef" in msg.body_text


@pytest.mark.parametrize("enc", ["utf-8", "cp1254"])
def test_8bit_body_without_charset_keeps_turkish(enc):
    text = "Maalesef şu an için uygun değil. İyi günler."
    raw = b"From: ik@alfa.com\r\nSubject: x\r\nContent-Type: text/plain\r\n\r\n" + text.encode(enc)
    msg = inbox.parse_message(raw)
    assert msg.body_text == text
    assert matcher.guess_sentiment(msg.body_text) == Sentiment.negative


def test_base64_and_quoted_printable_bodies():
    text = "Sizi mülakata davet ediyoruz; müsait olduğunuz günü yazın."
    b64 = base64.b64encode(text.encode("utf-8"))
    raw = b"From: ik@alfa.com\r\nSubject: x\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n" + b64 + b"\r\n"
    assert inbox.parse_message(raw).body_text == text
    raw = (
        b"From: ik@alfa.com\r\nSubject: x\r\nContent-Type: text/plain; charset=iso-8859-9\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\nMaalesef =FEu an i=E7in uygun de=F0il\r\n"
    )
    assert inbox.parse_message(raw).body_text == "Maalesef şu an için uygun değil"


def test_encoded_subject_header_is_decoded():
    m = Mime()
    m["From"] = "ik@alfa.com"
    m["Subject"] = "Yanıt: Staj Başvurusu [BV-0007]"
    m.set_content("x")
    raw = m.as_bytes()
    assert b"=?utf-8?" in raw.lower()
    msg = inbox.parse_message(raw)
    assert msg.subject == "Yanıt: Staj Başvurusu [BV-0007]" and msg.tracking_code == "BV-0007"


def test_html_only_mail_quote_is_marked_and_ignored_for_sentiment():
    html = (
        "<html><body><div dir='ltr'>Olumlu, görüşelim.</div><br>"
        "<div class='gmail_quote'>3 Kas 2026 tarihinde Ben &lt;ben@gmail.com&gt; şunu yazdı:<br>"
        "<blockquote class='gmail_quote'>Maalesef maalesef kontenjan [BV-0001] eski mesaj</blockquote></div></body></html>"
    )
    msg = inbox.parse_message(_mime(html=html))
    assert msg.body_html and msg.body_text.startswith("Olumlu, görüşelim.")
    # alıntı metinde '>' ile işaretlenir; takip kodu yine bulunur
    assert "> " in msg.body_text and "eski mesaj" in msg.body_text and msg.tracking_code == "BV-0001"
    assert matcher.reply_body(msg.body_text) == "Olumlu, görüşelim."
    assert matcher.guess_sentiment(msg.body_text) == Sentiment.positive


def test_html_outlook_reply_header_is_treated_as_quote():
    html = (
        '<div>Teşekkürler, uygun olur.</div><div id="appendonsend"></div><hr>'
        '<div id="divRplyFwdMsg"><b>Kimden:</b> Ben<br></div><div>maalesef maalesef maalesef</div>'
    )
    msg = inbox.parse_message(_mime(html=html))
    assert matcher.reply_body(msg.body_text) == "Teşekkürler, uygun olur."
    assert matcher.guess_sentiment(msg.body_text) == Sentiment.positive


def test_multipart_with_attachment_prefers_plain_text_part():
    m = Mime()
    m["From"] = "ik@alfa.com"
    m["Subject"] = "Belgeler"
    m.set_content("Lütfen ekteki formu doldurun.")
    m.add_alternative("<p>Lütfen <b>ekteki</b> formu doldurun.</p>", subtype="html")
    m.add_attachment(b"%PDF-1.4 sahte", maintype="application", subtype="pdf", filename="form.pdf")
    m.add_attachment("bu bir ek metin dosyasıdır", subtype="plain", filename="notlar.txt")
    msg = inbox.parse_message(m.as_bytes())
    assert msg.body_text == "Lütfen ekteki formu doldurun."
    assert msg.body_html and "<b>ekteki</b>" in msg.body_html
    assert "ek metin" not in (msg.body_text or "")
    assert matcher.guess_sentiment(msg.body_text) == Sentiment.info


def test_missing_date_and_from_and_naive_date():
    before = now_utc() - timedelta(seconds=5)
    msg = inbox.parse_message(_mime(date=None))
    assert msg.received_at >= before
    raw = b"Subject: x\r\nDate: Tue, 03 Nov 2026 10:00:00\r\n\r\nb"
    msg2 = inbox.parse_message(raw)
    assert msg2.from_addr is None and msg2.received_at == datetime(2026, 11, 3, 10, 0)
    msg3 = inbox.parse_message(_mime(date="bu bir tarih degil"))
    assert msg3.received_at >= before


def test_reply_to_and_auto_submitted_are_captured():
    raw = _mime(frm="noreply@kariyer.net", headers={"Reply-To": "IK <IK@Alfa.com>", "Auto-Submitted": "auto-replied"})
    msg = inbox.parse_message(raw)
    meta = inbox.inbound_meta(msg)
    assert meta.reply_to == "ik@alfa.com" and meta.auto_reply is True


# --- matcher: sıra ve eşleşme yolları ----------------------------------------------------

def test_in_reply_to_wins_over_tracking_code_of_other_company(session):
    a = _company(session, "Alfa", "alfa.com", msgid="<out-a@stajbot.local>")
    b = _company(session, "Beta", "beta.com", msgid="<out-b@stajbot.local>")
    msg = _stored(session, _mime(frm="x@gmail.com", subject=f"Re: [{b.tracking_code}]", in_reply_to="<out-a@stajbot.local>"))
    res = matcher.match(msg, session)
    assert res.company is a and res.method == MatchMethod.message_id
    assert msg.tracking_code == b.tracking_code or msg.tracking_code == a.tracking_code  # bilgi amaçlı


def test_references_with_many_ids_and_ours_in_middle(session):
    c = _company(session, msgid="<out1@stajbot.local>")
    # katlanmış (folded) başlık: ham baytlarla kurulur, Mime kurucusu CRLF kabul etmez
    raw = (
        b"From: x@gmail.com\r\nTo: ben@gmail.com\r\nSubject: Re:\r\nMessage-ID: <r1@x>\r\n"
        b"References: <x1@other> <x2@other>\r\n <out1@stajbot.local>\r\n <x3@other>\r\n\r\nSelam\r\n"
    )
    msg = _stored(session, raw)
    assert matcher.match(msg, session).company is c


def test_in_reply_to_without_angle_brackets(session):
    c = _company(session, msgid="<out1@stajbot.local>")
    msg = _stored(session, _mime(frm="x@gmail.com", subject="Re:", in_reply_to="out1@stajbot.local"))
    res = matcher.match(msg, session)
    assert res.company is c and res.method == MatchMethod.message_id


def test_reply_to_earlier_inbound_in_same_thread(session):
    c = _company(session)
    first = _stored(session, _mime(subject="İlk cevap", msgid="<in1@alfa.com>"))
    assert matcher.match(first, session).company is c
    # şirket, bizim Gmail'den elle attığımız cevaba cevap verir; References zinciri ilk mesajı içerir
    second = _stored(session, _mime(frm="baska@gmail.com", subject="Re:", msgid="<in2@x>", references="<gmail-reply@mail.gmail.com> <in1@alfa.com>"))
    res = matcher.match(second, session)
    assert res.company is c and res.method == MatchMethod.message_id


def test_tracking_code_lowercase_in_body(session):
    c = _company(session, msgid=None)
    msg = _stored(session, _mime(frm="x@gmail.com", subject="hk.", body="Merhaba,\nbv-0001 kodlu başvurunuz hakkında."))
    res = matcher.match(msg, session)
    assert res.company is c and res.method == MatchMethod.tracking_code and msg.tracking_code == "BV-0001"


def test_tracking_code_wrong_format_or_missing_company_falls_through(session):
    c = _company(session, msgid=None)
    m1 = _stored(session, _mime(frm="x@gmail.com", subject="BV-1 BV-12 BV 0001 BVX-0001", msgid="<m1@x>"))
    assert matcher.match(m1, session).company is None
    # kod var ama şirket yok -> domain adımına düşer
    m2 = _stored(session, _mime(frm="kisi@alfa.com", subject="Re: [BV-9999]", msgid="<m2@x>"))
    res = matcher.match(m2, session)
    assert res.company is c and res.method == MatchMethod.domain


def test_generic_domain_excluded_but_exact_contact_email_matches(session):
    c = _company(session, msgid=None, contact="ahmet.ik@gmail.com")
    m1 = _stored(session, _mime(frm="baska@gmail.com", msgid="<m1@x>"))
    assert matcher.match(m1, session).company is None
    m2 = _stored(session, _mime(frm="Ahmet.IK@gmail.com", msgid="<m2@x>"))
    res = matcher.match(m2, session)
    assert res.company is c and res.method == MatchMethod.domain


def test_subdomain_sender_matches_company_domain(session):
    c = _company(session, "Alfa", "alfa.com", msgid=None)
    m = _stored(session, _mime(frm="ik@mail.alfa.com", msgid="<m1@x>"))
    assert matcher.match(m, session).company is c
    c2 = _company(session, "Beta", "beta.com.tr", msgid=None)
    m2 = _stored(session, _mime(frm="hr@ik.beta.com.tr", msgid="<m2@x>"))
    assert matcher.match(m2, session).company is c2


def test_public_suffix_is_never_a_match_key(session):
    weird = Company(name="Kötü", domain="com.tr", city="Konya")
    session.add(weird)
    session.flush()
    m = _stored(session, _mime(frm="x@bilinmeyen.com.tr", msgid="<m1@x>"))
    assert matcher.match(m, session).company is None


def test_reply_to_domain_is_used_when_from_is_a_relay(session):
    c = _company(session, msgid=None)
    m = _stored(session, _mime(frm="noreply@kariyer.net", msgid="<m1@x>", headers={"Reply-To": "ik@alfa.com"}))
    res = matcher.match(m, session)
    assert res.company is c and res.method == MatchMethod.domain


def test_reply_to_generic_domain_is_still_excluded(session):
    _company(session, msgid=None)
    m = _stored(session, _mime(frm="noreply@kariyer.net", msgid="<m1@x>", headers={"Reply-To": "kisi@hotmail.com"}))
    assert matcher.match(m, session).company is None


def test_blank_or_invalid_from(session):
    _company(session, msgid=None)
    m = _stored(session, b"Subject: x\r\n\r\nbody")
    res = matcher.match(m, session)
    assert res.company is None and res.method == MatchMethod.none and m.sentiment == Sentiment.unknown


# --- matcher: durum geçişleri ---------------------------------------------------------------

@pytest.mark.parametrize("status", [AppStatus.sent, AppStatus.no_reply, AppStatus.followup_sent])
def test_reply_moves_to_replied(session, status):
    c = _company(session, status=status)
    m = _stored(session, _mime(in_reply_to="<out1@stajbot.local>"))
    matcher.match(m, session)
    assert c.status == AppStatus.replied
    ev = [e for e in c.events if e.type == EventType.status_change]
    assert ev and ev[0].old_status == status and ev[0].new_status == AppStatus.replied and ev[0].email_message_id == m.id


@pytest.mark.parametrize("status", [AppStatus.found, AppStatus.ready, AppStatus.drafted, AppStatus.queued, AppStatus.positive, AppStatus.negative, AppStatus.interview, AppStatus.offer, AppStatus.closed])
def test_reply_does_not_change_other_statuses(session, status):
    c = _company(session, status=status)
    m = _stored(session, _mime(in_reply_to="<out1@stajbot.local>"))
    matcher.match(m, session)
    assert c.status == status and m.company_id == c.id
    assert any(e.type == EventType.email_received for e in c.events)
    assert not any(e.type == EventType.status_change for e in c.events)


def test_last_contact_at_does_not_regress_for_older_mail(session):
    c = _company(session)
    c.last_contact_at = datetime(2026, 11, 10, 9, 0)
    m = _stored(session, _mime(in_reply_to="<out1@stajbot.local>", date="Tue, 03 Nov 2026 10:00:00 +0300"))
    matcher.match(m, session)
    assert c.last_contact_at == datetime(2026, 11, 10, 9, 0)
    m2 = _stored(session, _mime(msgid="<r2@alfa.com>", in_reply_to="<out1@stajbot.local>", date="Thu, 12 Nov 2026 10:00:00 +0300"))
    matcher.match(m2, session)
    assert c.last_contact_at == datetime(2026, 11, 12, 7, 0)


def test_auto_reply_is_matched_but_does_not_count_as_reply(session):
    c = _company(session)
    raw = _mime(subject="Otomatik yanıt: Staj Başvurusu", body="Ofis dışındayım, 10 Kasım'da döneceğim. Telefon ile ulaşabilirsiniz, görüşmek üzere.", headers={"Auto-Submitted": "auto-replied"})
    m = _stored(session, raw)
    res = matcher.match(m, session)
    assert res.company is c and c.status == AppStatus.sent
    assert m.sentiment == Sentiment.unknown and res.sentiment == Sentiment.unknown
    ev = [e for e in c.events if e.type == EventType.email_received]
    assert ev and "Otomatik" in ev[0].title


def test_auto_reply_detected_from_subject_without_header(session):
    c = _company(session)
    m = _stored(session, _mime(subject="Automatic reply: Staj", body="I am out of the office until Monday. Call me."))
    res = matcher.match(m, session)
    assert res.company is c and c.status == AppStatus.sent and m.sentiment == Sentiment.unknown


def test_unmatched_message_gets_sentiment_and_none_method(session):
    m = _stored(session, _mime(frm="x@gmail.com", body="Maalesef uygun değil"))
    res = matcher.match(m, session)
    assert res.company is None and m.matched_by == MatchMethod.none and m.sentiment == Sentiment.negative and m.company_id is None


# --- matcher: duygu tahmini --------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("MAALESEF BU DÖNEM KONTENJANIMIZ DOLMUŞTUR.", Sentiment.negative),
        ("Maalesef şu an için uygun değil.", Sentiment.negative),
        ("Görüşme için uygun musunuz? Hangi gün müsaitsiniz?", Sentiment.positive),
        ("Kontenjanımız var, sizi görüşmeye davet ediyoruz.", Sentiment.positive),
        ("Başvurunuz kabul edilmemiştir.", Sentiment.negative),
        ("Başvurunuz olumlu değerlendirilmemiştir. Başarılar dileriz.", Sentiment.negative),
        ("Bu dönem stajyer almıyoruz. Görüşmek üzere, iyi günler.", Sentiment.negative),
        ("Değerlendirme sürecimiz devam etmektedir, gerçekten teşekkürler.", Sentiment.unknown),
        ("Platformumuz üzerinden performans değerlendirmesi yapıyoruz.", Sentiment.unknown),
        ("Transkriptinizi ve okuldan alacağınız protokol belgesini iletebilir misiniz?", Sentiment.info),
        ("Unfortunately we have no open positions this term.", Sentiment.negative),
        ("We would like to schedule an interview; are you available on Monday?", Sentiment.positive),
        ("Sizi mülakata davet ediyoruz, lütfen CV'nizi de getirin.", Sentiment.positive),
        ("Staj tarihleriniz nedir, hangi bölümde okuyorsunuz?", Sentiment.info),
        ("Teşekkürler, iyi çalışmalar.", Sentiment.unknown),
        (None, Sentiment.unknown),
    ],
)
def test_guess_sentiment_cases(text, expected):
    assert matcher.guess_sentiment(text) == expected


def test_sentiment_ignores_quoted_text_and_signature():
    text = "Maalesef bu dönem alım yapmıyoruz.\n\n-- \nİK Birimi\nTelefon: 0212 000 00 00\nmülakat.com\n\n> Görüşme için davet"
    assert matcher.reply_body(text) == "Maalesef bu dönem alım yapmıyoruz."
    assert matcher.guess_sentiment(text) == Sentiment.negative
    wrapped = "Olumlu, görüşelim.\n3 Kas 2026 Sal 10:00 tarihinde Ben <ben@gmail.com> şunu\nyazdı:\n> maalesef maalesef"
    assert matcher.guess_sentiment(wrapped) == Sentiment.positive


def test_bottom_posted_reply_falls_back_to_full_text():
    text = "> Staj başvurum hk.\n> görüşme\n\nMaalesef kontenjanımız dolu."
    assert matcher.guess_sentiment(text) == Sentiment.negative


# --- fetch_new: sahte IMAP ------------------------------------------------------------------------

class FakeIMAP:
    """UID aramasında gerçek sunucu gibi davranır: aralık boşsa en son mesajı döner ('UID n:*' kuralı)."""

    mailbox: dict[int, bytes] = {}
    fail_login = False
    fetch_fail: set[int] = set()
    select_ok = True
    search_ok = True
    calls: list[tuple] = []
    logged_out = 0

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def login(self, u, p):
        if FakeIMAP.fail_login:
            raise Exception("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        FakeIMAP.calls.append(("select", folder, readonly))
        return ("OK", [b"3"]) if FakeIMAP.select_ok else ("NO", [b"yok"])

    def uid(self, cmd, *args):
        FakeIMAP.calls.append((cmd, args))
        if cmd == "search":
            if not FakeIMAP.search_ok:
                return "NO", [None]
            spec = args[-1]
            start = int(spec.split()[1].split(":")[0])
            ids = sorted(FakeIMAP.mailbox)
            if not ids:
                return "OK", [b""]
            sel = [u for u in ids if u >= start] or [ids[-1]]  # 'n:*' n > max ise sunucu en büyük UID'yi döner
            return "OK", [b" ".join(str(u).encode() for u in sel)]
        if cmd == "fetch":
            uid = int(args[0])
            if uid in FakeIMAP.fetch_fail:
                return "NO", [None]
            raw = FakeIMAP.mailbox.get(uid)
            if raw is None:
                return "NO", [None]
            return "OK", [(f"{uid} (UID {uid} RFC822 {{{len(raw)}}}".encode(), raw), b")"]
        raise AssertionError(cmd)

    def logout(self):
        FakeIMAP.logged_out += 1


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeIMAP.mailbox = {}
    FakeIMAP.fail_login = False
    FakeIMAP.fetch_fail = set()
    FakeIMAP.select_ok = True
    FakeIMAP.search_ok = True
    FakeIMAP.calls = []
    FakeIMAP.logged_out = 0
    yield


def _inbound_count(session) -> int:
    return len(session.scalars(select(EmailMessage).where(EmailMessage.direction == Direction.inbound)).all())


def test_first_run_without_last_uid_reads_everything_and_sets_watermark(session):
    FakeIMAP.mailbox = {5: _mime(msgid="<a@x>"), 9: _mime(msgid="<b@x>", frm="k@beta.com")}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert [m.imap_uid for m in new] == [5, 9]
    assert get_setting(session, inbox.LAST_UID_KEY) == "9"
    search = [c for c in FakeIMAP.calls if c[0] == "search"][0]
    assert search[1][-1].startswith("UID 1:* SINCE ")
    assert FakeIMAP.calls[0] == ("select", "INBOX", True)
    assert FakeIMAP.logged_out == 1


def test_uid_star_returns_last_message_again_and_is_deduped(session):
    FakeIMAP.mailbox = {7: _mime(msgid="<a@x>")}
    assert len(inbox.fetch_new(session, imap_factory=FakeIMAP)) == 1
    assert get_setting(session, inbox.LAST_UID_KEY) == "7"
    # ikinci çalıştırma: 'UID 8:*' sunucudan yine 7 döner -> işlenmez, UID geri gitmez
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    assert get_setting(session, inbox.LAST_UID_KEY) == "7" and _inbound_count(session) == 1
    # Message-ID'siz aynı UID tekrar gelirse de kaydedilmez
    FakeIMAP.mailbox[7] = _mime(msgid=None)
    set_setting(session, inbox.LAST_UID_KEY, "6")
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    assert _inbound_count(session) == 1


def test_max_messages_bound_processes_oldest_first_and_continues_next_run(session):
    FakeIMAP.mailbox = {u: _mime(msgid=f"<m{u}@x>", frm=f"k{u}@firma{u}.com") for u in range(1, 6)}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP, max_messages=2)
    assert [m.imap_uid for m in new] == [1, 2] and get_setting(session, inbox.LAST_UID_KEY) == "2"
    new = inbox.fetch_new(session, imap_factory=FakeIMAP, max_messages=2)
    assert [m.imap_uid for m in new] == [3, 4] and get_setting(session, inbox.LAST_UID_KEY) == "4"
    new = inbox.fetch_new(session, imap_factory=FakeIMAP, max_messages=2)
    assert [m.imap_uid for m in new] == [5] and get_setting(session, inbox.LAST_UID_KEY) == "5"
    assert _inbound_count(session) == 5


def test_own_sent_mail_is_skipped_case_insensitively(session, _settings, monkeypatch):
    monkeypatch.setattr(_settings, "mail_user", "Ben@Gmail.com")
    FakeIMAP.mailbox = {1: _mime(frm="Ahmet <BEN@gmail.com>", msgid="<a@x>"), 2: _mime(frm="k@beta.com", msgid="<b@x>")}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert [m.imap_uid for m in new] == [2] and get_setting(session, inbox.LAST_UID_KEY) == "2"


def test_message_id_dedupe_across_uids_and_folders(session):
    FakeIMAP.mailbox = {1: _mime(msgid="<same@x>"), 2: _mime(msgid="<same@x>", subject="kopya")}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert len(new) == 1 and _inbound_count(session) == 1 and get_setting(session, inbox.LAST_UID_KEY) == "2"


def test_failed_message_is_retried_once_then_skipped(session, monkeypatch):
    c = _company(session)
    FakeIMAP.mailbox = {
        1: _mime(msgid="<a@x>", frm="k@beta.com"),
        2: _mime(msgid="<b@x>", in_reply_to="<out1@stajbot.local>"),
        3: _mime(msgid="<c@x>", frm="k@gamma.com"),
    }
    real_store = inbox.store_message
    attempts: list[int] = []

    def boom(msg, session_):
        if msg.imap_uid == 2:
            attempts.append(2)
            session_.add(msg)
            session_.flush()  # kısmen yazılmış gibi; SAVEPOINT geri almalı
            raise RuntimeError("veritabanı patladı")
        return real_store(msg, session_)

    monkeypatch.setattr(inbox, "store_message", boom)
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    # 1 kaydedildi, 2 patladı -> işaret 2'yi geçmez, 3 bu turda işlenmez
    assert [m.imap_uid for m in new] == [1]
    assert get_setting(session, inbox.LAST_UID_KEY) == "1"
    assert _inbound_count(session) == 1  # yarım kayıt yok
    assert c.status == AppStatus.sent
    # ikinci tur: 2 yine patlar -> bu kez atlanır, 3 işlenir
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert [m.imap_uid for m in new] == [3] and attempts == [2, 2]
    assert get_setting(session, inbox.LAST_UID_KEY) == "3"
    # üçüncü tur: hiçbir şey yok
    monkeypatch.setattr(inbox, "store_message", real_store)
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    assert get_setting(session, inbox.LAST_UID_KEY) == "3"


def test_fetch_no_for_a_uid_is_retried_next_run(session):
    FakeIMAP.mailbox = {1: _mime(msgid="<a@x>", frm="k@beta.com"), 2: _mime(msgid="<b@x>", frm="k@gamma.com")}
    FakeIMAP.fetch_fail = {1}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert new == [] and (get_setting(session, inbox.LAST_UID_KEY) or "0") == "0"
    FakeIMAP.fetch_fail = set()
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert [m.imap_uid for m in new] == [1, 2] and get_setting(session, inbox.LAST_UID_KEY) == "2"


def test_garbage_message_in_middle_does_not_abort_batch(session):
    FakeIMAP.mailbox = {1: _mime(msgid="<a@x>", frm="k@beta.com"), 2: b"\xff\xfe\x00 garbage \x00", 3: _mime(msgid="<c@x>", frm="k@gamma.com")}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert {m.imap_uid for m in new} >= {1, 3} and get_setting(session, inbox.LAST_UID_KEY) == "3"


def test_select_or_search_failure_returns_empty_and_logs_out(session):
    FakeIMAP.mailbox = {1: _mime()}
    FakeIMAP.select_ok = False
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    FakeIMAP.select_ok = True
    FakeIMAP.search_ok = False
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    assert FakeIMAP.logged_out == 2 and get_setting(session, inbox.LAST_UID_KEY) is None


def test_empty_mailbox(session):
    assert inbox.fetch_new(session, imap_factory=FakeIMAP) == []
    assert get_setting(session, inbox.LAST_UID_KEY) is None


def test_fetch_new_end_to_end_matches_and_transitions(session):
    c = _company(session, status=AppStatus.no_reply)
    FakeIMAP.mailbox = {1: _mime(in_reply_to="<out1@stajbot.local>", body="Sizi mülakata davet ediyoruz.", html=None)}
    new = inbox.fetch_new(session, imap_factory=FakeIMAP)
    assert len(new) == 1 and new[0].company_id == c.id and new[0].matched_by == MatchMethod.message_id
    assert new[0].sentiment == Sentiment.positive and c.status == AppStatus.replied
    assert c.last_contact_at == new[0].received_at


def test_module_hygiene():
    import inspect

    for mod in (inbox, matcher):
        src = inspect.getsource(mod)
        assert "print(" not in src
        assert "get_engine" not in src and "create_engine" not in src


def test_imap_date_format_is_locale_independent():
    assert inbox._imap_date(datetime(2026, 11, 9, 7, 30)) == "9-Nov-2026"
    assert inbox._imap_date(datetime(2027, 1, 25)) == "25-Jan-2027"


def test_search_starts_one_day_before_first_sent_mail_not_whole_mailbox(session):
    c = _company(session, msgid=None)
    for d in (datetime(2026, 11, 20, 9, 0), datetime(2026, 11, 19, 6, 5)):
        session.add(EmailMessage(company_id=c.id, direction=Direction.outbound, status=MailStatus.sent, sent_at=d))
    session.add(EmailMessage(company_id=c.id, direction=Direction.outbound, status=MailStatus.draft))
    session.flush()
    assert inbox.since_date(session) == datetime(2026, 11, 18, 6, 5)
    FakeIMAP.mailbox = {3: _mime(msgid="<a@x>")}
    inbox.fetch_new(session, imap_factory=FakeIMAP)
    search = [c for c in FakeIMAP.calls if c[0] == "search"][0]
    assert search[1][-1] == "UID 1:* SINCE 18-Nov-2026"


def test_without_sent_mail_only_recent_days_are_read(session):
    got = inbox.since_date(session)
    assert timedelta(days=inbox.NO_SENT_LOOKBACK_DAYS - 1) < now_utc() - got <= timedelta(days=inbox.NO_SENT_LOOKBACK_DAYS, seconds=5)


# --- teslim edilemedi (bounce) ---------------------------------------------------------------

GMAIL_BOUNCE_BODY = (
    "** İleti engellendi **\n\nik@alfa.com adresine gönderdiğiniz ileti engellendi.\n\n"
    "Uzak sunucunun yanıtı şuydu:\n550 5.4.1 Recipient address rejected: Access denied.\n"
)


def _out(c):
    return [m for m in c.emails if m.direction == Direction.outbound][0]


@pytest.mark.parametrize(
    "frm,subject",
    [
        ("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)"),
        ("<postmaster@alfa.com>", "Undeliverable: Staj Başvurusu [BV-1]"),
        ("sistem@alfa.com", "Teslim edilemedi: Staj"),
    ],
)
def test_bounce_marks_outbound_failed_and_company_bounced(session, frm, subject):
    c = _company(session)
    m = _stored(session, _mime(frm=frm, subject=subject, body=GMAIL_BOUNCE_BODY, references="<out1@stajbot.local>"))
    res = matcher.match(m, session)
    assert res.company is c and res.auto_reply and m.sentiment == Sentiment.unknown
    assert c.status == AppStatus.bounced
    out = _out(c)
    assert out.status == MailStatus.failed and "5.4.1" in out.error
    assert any(e.title.startswith("Mail ulaşmadı (ik@alfa.com)") for e in c.events)


def test_bounce_without_thread_headers_uses_latest_sent_to_that_company(session):
    c = _company(session)
    c2 = _company(session, name="Beta", domain="beta.com", msgid="<out2@stajbot.local>")
    m = _stored(session, _mime(frm="postmaster@alfa.com", subject="Undeliverable: Staj", body="Your message couldn't be delivered.\n550 5.1.1 User unknown"))
    matcher.match(m, session)
    assert c.status == AppStatus.bounced and _out(c).status == MailStatus.failed
    assert c2.status == AppStatus.sent and _out(c2).status == MailStatus.sent


def test_bounce_does_not_override_later_statuses(session):
    c = _company(session, status=AppStatus.interview)
    m = _stored(session, _mime(frm="mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)", references="<out1@stajbot.local>"))
    matcher.match(m, session)
    assert c.status == AppStatus.interview


def test_normal_reply_is_not_a_bounce(session):
    c = _company(session)
    m = _stored(session, _mime(subject="Re: Staj", body="Merhaba, başvurunuzu aldık, mülakat için görüşelim.", in_reply_to="<out1@stajbot.local>"))
    matcher.match(m, session)
    assert c.status == AppStatus.replied and _out(c).status == MailStatus.sent


def test_human_reply_after_bounce_marks_company_replied(session):
    # info@ iç adrese yönlendirip geri döndü, ama İK başka adresten cevap yazdı (SFS vakası)
    c = _company(session, status=AppStatus.bounced)
    m = _stored(session, _mime(frm="ik@alfa.com", subject="Başvurunuz hakkında", body="Merhaba, başvurunuzu aldık."))
    matcher.match(m, session)
    assert c.status == AppStatus.replied


def test_bounce_reason_picks_smtp_code_line():
    assert matcher.bounce_reason(GMAIL_BOUNCE_BODY) == "İleti engellendi — 550 5.4.1 Recipient address rejected: Access denied."
    o365 = (
        "[https://products.office.com/logo.png]\nYour message to x@alfa.com couldn't be delivered.\n"
        "* Clear the Auto-Complete List, see error code 5.4.14\nStatus code 554 5.4.14\nError: 554 5.4.14 Hop count exceeded\n"
    )
    assert matcher.bounce_reason(o365) == "Your message to x@alfa.com couldn't be delivered. — Status code 554 5.4.14"
    assert matcher.bounce_reason("") == "sebep belirtilmemiş"
