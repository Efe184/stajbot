from stajbot import services
from stajbot.enums import Actor, AppStatus, ContactKind, EventType
from stajbot.models import Company


def test_add_contact_marks_ready_when_approved(company):
    assert company.status == AppStatus.ready
    assert company.primary_contact.email == "kariyer@ornekyazilim.com"
    assert company.primary_contact.kind == ContactKind.hr


def test_system_transition_guard(session, company):
    try:
        services.change_status(session, company, AppStatus.offer, actor=Actor.system)
    except ValueError:
        pass
    else:
        raise AssertionError("sistem izinsiz geçiş yapamamalı")
    ev = services.change_status(session, company, AppStatus.offer, actor=Actor.user, body="telefonla teklif")
    assert ev.type == EventType.status_change and company.status == AppStatus.offer


def test_better_contact_becomes_primary(session):
    c = Company(name="X", domain="x.com", city="Ankara")
    session.add(c)
    session.flush()
    services.add_contact(session, c, "info@x.com", source="test")
    assert c.primary_contact.email == "info@x.com"
    services.add_contact(session, c, "ik@x.com", source="test")
    assert c.primary_contact.email == "ik@x.com"
    assert services.add_contact(session, c, "gecersiz", source="test") is None


def test_classify_contact_kind():
    assert services.classify_contact_kind("hr@a.com") == ContactKind.hr
    assert services.classify_contact_kind("ahmet.yilmaz@a.com") == ContactKind.personal
    assert services.classify_contact_kind("info@a.com") == ContactKind.info
