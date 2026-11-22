"""SQLAlchemy modelleri (paylaşılan sözleşme). Detay: MIMARI.md bölüm 4."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    Actor,
    AppStatus,
    ContactKind,
    Direction,
    EventType,
    MailStatus,
    MatchMethod,
    ReviewStatus,
    Sentiment,
    SizeBucket,
)
from .utils import now_utc


class Base(DeclarativeBase):
    pass


def _enum(e: type) -> Enum:
    # Değerleri (name değil value) sakla; SQLite'ta string olarak tutulur.
    return Enum(e, native_enum=False, values_callable=lambda x: [m.value for m in x], length=32)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    website: Mapped[str | None] = mapped_column(String(512))
    city: Mapped[str | None] = mapped_column(String(64), index=True)
    district: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(64))
    sector: Mapped[str | None] = mapped_column(String(128), index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    tech_stack: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    size_bucket: Mapped[SizeBucket] = mapped_column(_enum(SizeBucket), default=SizeBucket.unknown, nullable=False)
    employee_count: Mapped[int | None] = mapped_column(Integer)
    founded_year: Mapped[int | None] = mapped_column(Integer)
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    career_url: Mapped[str | None] = mapped_column(String(512))
    has_internship_posting: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    internship_posting_url: Mapped[str | None] = mapped_column(String(512))
    is_arge_center: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_teknopark: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    teknopark_name: Mapped[str | None] = mapped_column(String(128))
    school_protocol: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    review_status: Mapped[ReviewStatus] = mapped_column(
        _enum(ReviewStatus), default=ReviewStatus.pending, nullable=False, index=True
    )
    status: Mapped[AppStatus] = mapped_column(_enum(AppStatus), default=AppStatus.found, nullable=False, index=True)
    blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc, nullable=False)

    sources: Mapped[list["CompanySource"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", order_by="CompanySource.collected_at"
    )
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", order_by="Contact.priority"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", order_by="Event.occurred_at.desc()"
    )
    emails: Mapped[list["EmailMessage"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", order_by="EmailMessage.created_at.desc()"
    )

    # --- yardımcılar ---------------------------------------------------------
    @property
    def primary_contact(self) -> "Contact | None":
        for c in self.contacts:
            if c.is_primary:
                return c
        return self.contacts[0] if self.contacts else None

    @property
    def tracking_code(self) -> str:
        return f"BV-{self.id:04d}"

    @property
    def source_names(self) -> list[str]:
        return [s.source for s in self.sources]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Company {self.id} {self.name!r} {self.city} {self.status.value}>"


class CompanySource(Base):
    __tablename__ = "company_sources"
    __table_args__ = (UniqueConstraint("company_id", "source", name="uq_company_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024))
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)

    company: Mapped[Company] = relationship(back_populates="sources")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("company_id", "email", name="uq_contact_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[ContactKind] = mapped_column(_enum(ContactKind), default=ContactKind.other, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)

    company: Mapped[Company] = relationship(back_populates="contacts")

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower() if "@" in self.email else ""


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    type: Mapped[EventType] = mapped_column(_enum(EventType), nullable=False)
    old_status: Mapped[AppStatus | None] = mapped_column(_enum(AppStatus))
    new_status: Mapped[AppStatus | None] = mapped_column(_enum(AppStatus))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False, index=True)
    created_by: Mapped[Actor] = mapped_column(_enum(Actor), default=Actor.system, nullable=False)
    email_message_id: Mapped[int | None] = mapped_column(ForeignKey("email_messages.id", ondelete="SET NULL"))

    company: Mapped[Company] = relationship(back_populates="events")
    email_message: Mapped["EmailMessage | None"] = relationship(foreign_keys=[email_message_id])


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    direction: Mapped[Direction] = mapped_column(_enum(Direction), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(512))
    references_header: Mapped[str | None] = mapped_column(Text)
    tracking_code: Mapped[str | None] = mapped_column(String(16), index=True)
    from_addr: Mapped[str | None] = mapped_column(String(255))
    to_addr: Mapped[str | None] = mapped_column(String(255))
    cc: Mapped[str | None] = mapped_column(String(512))
    subject: Mapped[str | None] = mapped_column(String(512))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MailStatus] = mapped_column(_enum(MailStatus), default=MailStatus.draft, nullable=False, index=True)
    imap_uid: Mapped[int | None] = mapped_column(Integer)
    matched_by: Mapped[MatchMethod] = mapped_column(_enum(MatchMethod), default=MatchMethod.none, nullable=False)
    sentiment: Mapped[Sentiment] = mapped_column(_enum(Sentiment), default=Sentiment.unknown, nullable=False)
    llm_paragraph: Mapped[str | None] = mapped_column(Text)
    template_name: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)

    company: Mapped[Company | None] = relationship(back_populates="emails")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
