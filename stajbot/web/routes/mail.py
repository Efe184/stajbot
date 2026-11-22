"""Mail taslakları: önizleme, düzenleme, paragrafı yeniden üretme, onay, silme, gönderimi başlatma."""
from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import db, services
from ...config import get_settings
from ...enums import Actor, AppStatus, Direction, EventType, MailStatus
from ...log import get_logger
from ...models import Company, EmailMessage
from ...utils import clean_text
from ..deps import get_db

log = get_logger(__name__)
router = APIRouter()

TABS = ("draft", "approved", "sent", "failed")
_send_lock = threading.Lock()
_last_send_report: dict = {}


def _get_msg(session: Session, msg_id: int) -> EmailMessage:
    m = session.get(EmailMessage, msg_id)
    if m is None or m.direction != Direction.outbound:
        raise HTTPException(status_code=404, detail="Mesaj bulunamadı")
    return m


def _int(value: Optional[str]) -> int | None:
    try:
        return int(str(value).strip()) if value is not None and str(value).strip() else None
    except ValueError:
        return None


@router.get("/mailler")
def mails(request: Request, tab: str = "draft", company: Optional[str] = None, session: Session = Depends(get_db)):
    from ..app import render

    tab = tab if tab in TABS else "draft"
    company = _int(company)  # "?company=" gibi boş değer 422 yerine filtre yok sayılır
    stmt = select(EmailMessage).where(EmailMessage.direction == Direction.outbound, EmailMessage.status == MailStatus(tab))
    if company:
        stmt = stmt.where(EmailMessage.company_id == company)
    order = EmailMessage.sent_at.desc() if tab == "sent" else EmailMessage.created_at.desc()
    rows = list(session.scalars(stmt.order_by(order, EmailMessage.id.desc()).limit(200)))
    counts = {t: 0 for t in TABS}
    for st, n in session.execute(select(EmailMessage.status, func.count()).where(EmailMessage.direction == Direction.outbound).group_by(EmailMessage.status)).all():
        if st.value in counts:
            counts[st.value] = n
    ready = session.scalar(select(func.count()).select_from(Company).where(Company.status == AppStatus.ready, Company.blacklisted.is_(False))) or 0
    return render(request, "mails.html", messages=rows, tab=tab, counts=counts, ready_count=ready, company=company, last_report=_last_send_report, settings=get_settings())


def _card(request: Request, m: EmailMessage, msg: str | None = None):
    from ..app import render

    return render(request, "_mail_card.html", m=m, msg=msg)


def _apply_edits(m: EmailMessage, subject: str | None, body_text: str | None) -> None:
    """Karttaki konu/gövde alanları (varsa) mesaja yazılır."""
    if subject is not None and clean_text(subject, 512):
        m.subject = clean_text(subject, 512)
    if body_text is not None and body_text.strip():
        m.body_text = body_text.replace("\r\n", "\n").strip() + "\n"


def _tracking_warning(m: EmailMessage) -> str:
    code = m.tracking_code or (m.company.tracking_code if m.company is not None else None)
    if code and code not in (m.subject or "") and code not in (m.body_text or ""):
        return f" Uyarı: takip kodu {code} konu ve gövdede yok; cevap eşleştirmesi yalnızca alan adına dayanır."
    return ""


@router.post("/mailler/{msg_id}/onayla")
def approve(request: Request, msg_id: int, subject: Optional[str] = Form(None), body_text: Optional[str] = Form(None), session: Session = Depends(get_db)):
    from ..app import is_htmx, redirect

    m = _get_msg(session, msg_id)
    if m.status != MailStatus.draft:
        raise HTTPException(status_code=400, detail="Yalnızca taslaklar onaylanır")
    # Karttaki "Onayla" düğmesi formla birlikte gelir: kaydedilmemiş düzenlemeler kaybolmasın.
    _apply_edits(m, subject, body_text)
    if not (m.to_addr or "").strip():
        raise HTTPException(status_code=400, detail="Alıcı adresi yok; önce şirkete e-posta ekleyin ve taslağı yeniden üretin")
    m.status = MailStatus.approved
    c = m.company
    if c is not None:
        if c.status == AppStatus.drafted:
            services.change_status(session, c, AppStatus.queued, actor=Actor.user, title="Taslak onaylandı, gönderim kuyruğunda", email_message=m)
        else:
            services.add_event(session, c, EventType.manual, "Mail taslağı onaylandı", actor=Actor.user, email_message=m)
    session.flush()
    warn = _tracking_warning(m)
    if is_htmx(request):
        return _card(request, m, "Onaylandı; 'send' komutu ya da Gönderimi başlat ile gider." + warn)
    return redirect("/mailler?tab=approved", "Taslak onaylandı." + warn)


@router.post("/mailler/{msg_id}/geri-al")
def unapprove(request: Request, msg_id: int, session: Session = Depends(get_db)):
    from ..app import is_htmx, redirect

    m = _get_msg(session, msg_id)
    if m.status not in (MailStatus.approved, MailStatus.failed):
        raise HTTPException(status_code=400, detail="Bu mesaj taslağa döndürülemez")
    m.status = MailStatus.draft
    m.error = None
    c = m.company
    if c is not None and c.status == AppStatus.queued:
        services.change_status(session, c, AppStatus.drafted, actor=Actor.user, title="Onay geri alındı", email_message=m)
    session.flush()
    if is_htmx(request):
        return _card(request, m, "Taslağa döndürüldü")
    return redirect("/mailler?tab=draft", "Taslağa döndürüldü")


@router.post("/mailler/{msg_id}/duzenle")
def edit(request: Request, msg_id: int, subject: str = Form(...), body_text: str = Form(...), session: Session = Depends(get_db)):
    from ..app import is_htmx, redirect

    m = _get_msg(session, msg_id)
    if m.status not in (MailStatus.draft, MailStatus.approved):
        raise HTTPException(status_code=400, detail="Gönderilmiş mesaj düzenlenemez")
    if not body_text.strip():
        if is_htmx(request):
            return _card(request, m, "Gövde boş olamaz; kaydedilmedi")
        return redirect(f"/mailler?tab={m.status.value}", "Gövde boş olamaz; kaydedilmedi")
    _apply_edits(m, subject, body_text)
    session.flush()
    warn = _tracking_warning(m)
    if is_htmx(request):
        return _card(request, m, "Kaydedildi." + warn)
    return redirect(f"/mailler?tab={m.status.value}", "Mesaj kaydedildi." + warn)


@router.post("/mailler/{msg_id}/yeniden-uret")
def regenerate(request: Request, msg_id: int, use_llm: bool = Form(True), session: Session = Depends(get_db)):
    from ..app import is_htmx, redirect
    from ...mail import compose

    m = _get_msg(session, msg_id)
    if m.status != MailStatus.draft:
        raise HTTPException(status_code=400, detail="Yalnızca taslaklar yeniden üretilir")
    c = m.company
    if c is None:
        raise HTTPException(status_code=400, detail="Mesajın şirketi yok")
    if compose.is_followup(m):
        res = compose.compose_followup(c, session)
    else:
        res = compose.compose_for_company(c, session, use_llm=bool(use_llm))
    if not res.ok:
        if is_htmx(request):
            return _card(request, m, f"Üretilemedi: {res.error}")
        return redirect("/mailler", f"Üretilemedi: {res.error}")
    new = res.message
    note = "Yeniden üretildi" + (" (LLM paragrafı)" if res.used_llm else " (şablon paragrafı)")
    if is_htmx(request):
        return _card(request, new, note)
    return redirect("/mailler", note)


@router.post("/mailler/{msg_id}/sil")
def delete(request: Request, msg_id: int, session: Session = Depends(get_db)):
    from ..app import is_htmx, redirect
    from fastapi.responses import HTMLResponse

    m = _get_msg(session, msg_id)
    if m.status not in (MailStatus.draft, MailStatus.approved, MailStatus.failed):
        raise HTTPException(status_code=400, detail="Gönderilmiş mesaj silinemez")
    c = m.company
    tab = m.status.value
    session.delete(m)
    session.flush()
    if c is not None:
        session.refresh(c)
        others = [x for x in c.emails if x.direction == Direction.outbound and x.status in (MailStatus.draft, MailStatus.approved)]
        if not others and c.status in (AppStatus.drafted, AppStatus.queued):
            services.change_status(session, c, AppStatus.ready, actor=Actor.user, title="Taslak silindi; yeniden Hazır")
    if is_htmx(request):
        return HTMLResponse("")
    return redirect(f"/mailler?tab={tab}", "Mesaj silindi")


def _run_send(dry_run: bool, force: bool, limit: int | None) -> None:
    from ...mail import sender

    if not _send_lock.acquire(blocking=False):
        log.warning("Gönderim zaten çalışıyor; yeni istek yok sayıldı")
        return
    try:
        with db.get_session() as session:
            rep = sender.send_approved(session, dry_run=dry_run, force=force, limit=limit)
        _last_send_report.clear()
        _last_send_report.update(sent=rep.sent, skipped=rep.skipped, failed=rep.failed, reason=rep.reason, mode=rep.mode, dry_run=rep.dry_run, errors=rep.errors[:10])
    except Exception as e:  # noqa: BLE001
        log.error("Arka plan gönderimi hatası: %s", e)
        _last_send_report.clear()
        _last_send_report.update(error=str(e))
    finally:
        _send_lock.release()


@router.post("/mailler/gonder")
def start_sending(request: Request, background: BackgroundTasks, dry_run: bool = Form(True), force: bool = Form(False), limit: Optional[str] = Form(None)):
    """Gönderimi arka planda başlatır.

    Güvenlik: ``dry_run`` varsayılanı **True** — alan hiç gönderilmezse prova yapılır; gerçek gönderim için
    form açıkça ``dry_run=false`` göndermelidir (mails.html'deki seçim kutusu varsayılan olarak provadır).
    """
    from ..app import is_htmx, redirect, render

    s = get_settings()
    lim = _int(limit)
    if not dry_run and s.mail_mode != "dry" and not s.mail_configured:
        msg = "MAIL_USER / MAIL_PASSWORD tanımlı değil (.env)"
    elif _send_lock.locked():
        msg = "Gönderim zaten çalışıyor"
    else:
        background.add_task(_run_send, bool(dry_run), bool(force), lim)
        msg = f"Gönderim arka planda başladı (mod: {s.mail_mode}{', prova' if dry_run else ''}). Sayfayı yenileyerek sonucu görün."
    if is_htmx(request):
        return render(request, "_notice.html", msg=msg)
    return redirect("/mailler?tab=approved", msg)
