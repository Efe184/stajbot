"""Ayarlar: .env dosyasından okunur (paylaşılan sözleşme). Bkz. MIMARI.md bölüm 12."""
from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .enums import TARGET_CITIES

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Genel
    app_name: str = "Staj Başvuru Otomasyonu"
    db_path: Path = Path("data/stajbot.db")
    data_dir: Path = Path("data")
    log_level: str = "INFO"

    # Kullanıcı
    user_full_name: str = "Ad Soyad"
    user_phone: str = ""
    user_school: str = ""
    user_department: str = "Bilgisayar Mühendisliği"
    user_class: str = "4. sınıf"
    internship_term: str = "2026-2027 Bahar"
    internship_start: str = ""
    internship_end: str = ""
    profile_path: Path = Path("data/profil.md")

    # Hedef şehirler (virgülle ayrılmış). Boşsa enums.TARGET_CITIES.
    target_cities: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(TARGET_CITIES))

    # Tarama
    http_user_agent: str = "StajBot/0.1 (+ogrenci staj basvuru otomasyonu)"
    crawl_delay_sec: float = 1.5
    http_timeout_sec: float = 20.0

    # Dış servisler
    google_places_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_max_tokens: int = 400

    # E-posta
    mail_mode: Literal["dry", "draft", "send"] = "dry"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    mail_user: str = ""
    mail_password: str = ""
    mail_from_name: str = ""
    daily_send_limit: int = 15
    send_delay_min_sec: float = 120
    send_delay_max_sec: float = 360
    send_window: str = "09:00-17:30"
    send_weekdays_only: bool = True
    followup_days: int = 10
    min_days_between_mails: int = 14
    attachments: Annotated[list[Path], NoDecode] = Field(default_factory=lambda: [Path("data/cv.pdf")])

    @field_validator("target_cities", mode="before")
    @classmethod
    def _split_cities(cls, v):
        if isinstance(v, str):
            items = [s.strip() for s in v.split(",") if s.strip()]
            return items or list(TARGET_CITIES)
        return v or list(TARGET_CITIES)

    @field_validator("attachments", mode="before")
    @classmethod
    def _split_paths(cls, v):
        if isinstance(v, str):
            return [Path(s.strip()) for s in v.split(",") if s.strip()]
        return v

    # --- türetilmiş ------------------------------------------------------------
    @property
    def db_url(self) -> str:
        p = self.db_path if self.db_path.is_absolute() else PROJECT_ROOT / self.db_path
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{p.as_posix()}"

    @property
    def abs_data_dir(self) -> Path:
        p = self.data_dir if self.data_dir.is_absolute() else PROJECT_ROOT / self.data_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def abs_profile_path(self) -> Path:
        return self.profile_path if self.profile_path.is_absolute() else PROJECT_ROOT / self.profile_path

    @property
    def abs_attachments(self) -> list[Path]:
        out = []
        for a in self.attachments:
            p = a if a.is_absolute() else PROJECT_ROOT / a
            if p.exists():
                out.append(p)
        return out

    @property
    def send_window_times(self) -> tuple[time, time]:
        try:
            a, b = self.send_window.split("-")
            h1, m1 = (int(x) for x in a.strip().split(":"))
            h2, m2 = (int(x) for x in b.strip().split(":"))
            return time(h1, m1), time(h2, m2)
        except Exception:
            return time(9, 0), time(17, 30)

    @property
    def mail_configured(self) -> bool:
        return bool(self.mail_user and self.mail_password)

    @property
    def llm_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    def masked(self) -> dict[str, str]:
        """Arayüzde göstermek için gizli alanları maskeler."""
        secret = {"google_places_api_key", "anthropic_api_key", "mail_password"}
        out: dict[str, str] = {}
        for k, v in self.model_dump().items():
            if k in secret:
                out[k] = ("•" * 8 + str(v)[-4:]) if v else "(boş)"
            else:
                out[k] = str(v)
        return out


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings
