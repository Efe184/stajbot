"""Ayarlar: .env özeti (maskeli, salt okunur) + profil.md düzenleme."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request

from ...config import get_settings
from ...log import get_logger
from ...mail import llm

log = get_logger(__name__)
router = APIRouter()

_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Genel", ("app_name", "db_path", "data_dir", "log_level", "target_cities")),
    ("Kullanıcı", ("user_full_name", "user_phone", "user_school", "user_department", "user_class", "internship_term", "internship_start", "internship_end", "profile_path")),
    ("Tarama", ("http_user_agent", "crawl_delay_sec", "http_timeout_sec")),
    ("Dış servisler", ("google_places_api_key", "anthropic_api_key", "llm_model", "llm_max_tokens")),
    ("E-posta", ("mail_mode", "smtp_host", "smtp_port", "imap_host", "imap_port", "mail_user", "mail_password", "mail_from_name", "daily_send_limit", "send_delay_min_sec", "send_delay_max_sec", "send_window", "send_weekdays_only", "followup_days", "min_days_between_mails", "attachments")),
)


@router.get("/ayarlar")
def settings_view(request: Request):
    from ..app import render

    s = get_settings()
    masked = s.masked()
    groups = [(title, [(k, masked.get(k, "")) for k in keys if k in masked]) for title, keys in _GROUPS]
    path = s.abs_profile_path
    profile = llm.load_profile(required=False)
    example = ""
    if not profile:
        try:
            example = llm.EXAMPLE_PROFILE.read_text(encoding="utf-8")
        except OSError:
            example = ""
    return render(
        request, "settings.html", groups=groups, profile=profile, profile_path=str(path), profile_exists=path.exists(), example=example,
        attachments=[str(p) for p in s.abs_attachments], mail_ok=s.mail_configured, llm_ok=s.llm_configured, places_ok=bool(s.google_places_api_key),
    )


@router.post("/ayarlar/profil")
def save_profile(text: str = Form(...)):
    """Profil metnini yalnızca ``settings.abs_profile_path`` dosyasına (UTF-8) yazar; yol istemciden alınmaz."""
    from ..app import redirect

    body = text.replace("\r\n", "\n").strip()
    if not body:
        return redirect("/ayarlar", "Profil boş olamaz; kaydedilmedi")
    if len(body) > llm.MAX_PROFILE_CHARS:
        return redirect("/ayarlar", f"Profil çok uzun (en çok {llm.MAX_PROFILE_CHARS} karakter); kaydedilmedi")
    p = llm.save_profile(body)
    log.info("Profil kaydedildi: %s", p)
    return redirect("/ayarlar", "Profil kaydedildi")
