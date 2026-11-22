"""mail/compose.py ve mail/llm.py testleri (LLM çağrısı sahte istemciyle)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from stajbot import services
from stajbot.config import get_settings
from stajbot.enums import AppStatus, Direction, EventType, MailStatus, ReviewStatus, SizeBucket
from stajbot.mail import compose, llm
from stajbot.models import Company, EmailMessage


@pytest.fixture(autouse=True)
def _profile(tmp_path, monkeypatch):
    p = tmp_path / "profil.md"
    p.write_text("# Profil\nPython, FastAPI, React ve Docker ile projeler.\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "profile_path", p)
    yield p


def test_pick_template(session):
    assert compose.pick_template(Company(name="A", sector="Savunma")) == "defense"
    assert compose.pick_template(Company(name="A", tags=["oyun"])) == "game"
    assert compose.pick_template(Company(name="A", size_bucket=SizeBucket.s11_50)) == "startup"
    assert compose.pick_template(Company(name="A", is_arge_center=True)) == "corporate"
    assert compose.pick_template(Company(name="A", size_bucket=SizeBucket.s1000_plus)) == "corporate"
    assert compose.pick_template(Company(name="A")) == "default"
    assert set(compose.available_templates()) == set(compose.TEMPLATE_NAMES)


def test_all_templates_render(company):
    for name in compose.TEMPLATE_NAMES:
        text = compose.render(name, **compose._context(company, why_paragraph=None, profile="Python", extra={"first_sent_at": "01.01.2026"}))
        assert compose.short_name(company.name) in text and company.tracking_code in text
        assert "{{" not in text and "{%" not in text
        assert "\n\n\n" not in text


def test_compose_no_llm_creates_draft_and_transitions(company, session):
    assert company.status == AppStatus.ready
    res = compose.compose_for_company(company, session, use_llm=False)
    assert res.ok and res.template_name == "default" and not res.used_llm
    msg = res.message
    assert msg.status == MailStatus.draft and msg.direction == Direction.outbound
    assert msg.to_addr == "kariyer@ornekyazilim.com" and msg.tracking_code == company.tracking_code
    assert company.tracking_code in msg.subject and "Uzun Dönem Staj Başvurusu" in msg.subject
    assert "Örnek Yazılım" in msg.body_text and "Python" in msg.body_text  # profil teknolojileri
    assert company.status == AppStatus.drafted
    assert any(e.type == EventType.status_change and e.new_status == AppStatus.drafted for e in company.events)


def test_compose_overwrites_existing_draft(company, session):
    r1 = compose.compose_for_company(company, session, use_llm=False)
    r1.message.body_text = "kullanıcı düzenledi"
    r2 = compose.compose_for_company(company, session, use_llm=False)
    assert r1.message.id == r2.message.id
    msgs = session.scalars(select(EmailMessage).where(EmailMessage.company_id == company.id)).all()
    assert len(msgs) == 1 and "kullanıcı düzenledi" not in msgs[0].body_text
    assert company.status == AppStatus.drafted


def test_compose_without_contact_fails(session):
    c = Company(name="Kişisiz", city="Konya", review_status=ReviewStatus.approved)
    session.add(c)
    session.flush()
    res = compose.compose_for_company(c, session, use_llm=False)
    assert not res.ok and "e-posta" in res.error
    assert session.scalars(select(EmailMessage)).all() == []


def test_compose_refuses_blacklisted_and_closed(company, session):
    company.blacklisted = True
    assert not compose.compose_for_company(company, session, use_llm=False).ok
    company.blacklisted = False
    services.change_status(session, company, AppStatus.closed)
    assert "closed" in compose.compose_for_company(company, session, use_llm=False).error


def test_compose_llm_used_when_configured(company, session, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", "test-key")
    calls = {}

    def fake(comp, profile):
        calls["company"] = comp.name
        calls["profile"] = profile
        return "Ödeme sistemleri alanındaki Python tabanlı çalışmalarınız profilimle örtüşüyor."

    monkeypatch.setattr(llm, "generate_why_paragraph", fake)
    res = compose.compose_for_company(company, session, use_llm=True)
    assert res.ok and res.used_llm and calls["company"] == company.name and "Python" in calls["profile"]
    assert res.message.llm_paragraph.startswith("Ödeme sistemleri") and "Ödeme sistemleri alanındaki" in res.message.body_text


def test_compose_llm_error_falls_back(company, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")

    def boom(comp, profile):
        raise llm.LLMError("ağ yok")

    monkeypatch.setattr(llm, "generate_why_paragraph", boom)
    res = compose.compose_for_company(company, session, use_llm=True)
    assert res.ok and not res.used_llm and res.message.llm_paragraph is None
    assert "çalışmalarını ilgiyle takip ediyorum" in res.message.body_text


def test_compose_without_api_key_uses_template(company, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    res = compose.compose_for_company(company, session, use_llm=True)
    assert res.ok and not res.used_llm


def test_compose_found_company_approved_becomes_drafted(session):
    c = Company(name="Yeni", city="Konya", review_status=ReviewStatus.approved, website="https://yeni.com")
    session.add(c)
    session.flush()
    # kişi ekleme ready yapar; found'da kalan senaryo için elle geri al
    services.add_contact(session, c, "ik@yeni.com", source="t")
    c.status = AppStatus.found
    res = compose.compose_for_company(c, session, use_llm=False)
    assert res.ok and c.status == AppStatus.drafted


def test_compose_followup_chains_to_last_sent(company, session):
    from datetime import datetime

    sent = EmailMessage(company=company, direction=Direction.outbound, status=MailStatus.sent, subject=compose.build_subject(company),
                        message_id="<abc@ornek>", to_addr="kariyer@ornekyazilim.com", sent_at=datetime(2026, 1, 5, 9, 0), tracking_code=company.tracking_code)
    session.add(sent)
    company.status = AppStatus.no_reply
    session.flush()
    res = compose.compose_followup(company, session)
    assert res.ok and res.template_name == "followup"
    m = res.message
    assert m.subject.startswith("Re: ") and company.tracking_code in m.subject
    assert m.in_reply_to == "<abc@ornek>" and "<abc@ornek>" in m.references_header
    assert "05.01.2026" in m.body_text and m.status == MailStatus.draft
    assert company.status == AppStatus.no_reply  # gönderilince followup_sent olur


def test_compose_followup_without_sent(company, session):
    # Mail araç dışından gönderilmiş, kullanıcı durumu elle 'no_reply' yapmış: gönderim kaydı yok ama hatırlatma üretilir.
    company.status = AppStatus.no_reply
    res = compose.compose_followup(company, session)
    assert res.ok and res.message.in_reply_to is None and "Kısa süre önce" in res.message.body_text


# --- llm ----------------------------------------------------------------------------

def test_load_profile_missing(tmp_path):
    p = tmp_path / "yok.md"
    assert llm.load_profile(required=False, path=p) == ""
    with pytest.raises(llm.ProfileMissingError, match="profil.example.md"):
        llm.load_profile(path=p)


def test_save_and_load_profile(tmp_path):
    p = tmp_path / "x" / "profil.md"
    llm.save_profile("  merhaba  ", p)
    assert llm.load_profile(path=p) == "merhaba"


def test_generate_why_paragraph_with_fake_client(company, monkeypatch):
    s = get_settings()
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='"Ödeme sistemleri alanında Python kullanmanız ilgimi çekiyor."\n')])

    client = SimpleNamespace(messages=FakeMessages())
    text = llm.generate_why_paragraph(company, "Python biliyorum", client=client)
    assert text == "Ödeme sistemleri alanında Python kullanmanız ilgimi çekiyor."
    assert captured["model"] == s.llm_model and captured["max_tokens"] >= 1500
    assert company.name in captured["messages"][0]["content"] and "Python biliyorum" in captured["messages"][0]["content"]
    assert "Fintech" in captured["messages"][0]["content"]


def test_generate_why_paragraph_errors(company):
    class Refusing:
        def create(self, **kw):
            return SimpleNamespace(stop_reason="refusal", content=[])

    class Empty:
        def create(self, **kw):
            return SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="thinking", thinking="...")])

    class Broken:
        def create(self, **kw):
            raise ConnectionError("ağ")

    for cls in (Refusing, Empty, Broken):
        with pytest.raises(llm.LLMError):
            llm.generate_why_paragraph(company, "p", client=SimpleNamespace(messages=cls()))


def test_generate_without_key(company, monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.generate_why_paragraph(company, "p")


def test_no_print():
    import inspect

    for mod in (compose, llm):
        assert "print(" not in inspect.getsource(mod)
