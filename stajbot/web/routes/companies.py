"""Şirket listesi, CSV dışa aktarma ve şirket detay sayfası (durum, not, kişi, alan düzenleme, kara liste)."""
from __future__ import annotations

import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ... import services
from ...enums import APP_STATUS_LABELS, Actor, AppStatus, ContactKind, ReviewStatus, SizeBucket, normalize_city, size_bucket_for
from ...log import get_logger
from ...models import Company, CompanySource, Contact
from ...utils import clean_text, domain_of, normalize_url, now_utc
from ..deps import get_db

log = get_logger(__name__)
router = APIRouter()

PAGE_SIZE = 50
SORTS = {
    "score": Company.score.desc(),
    "name": Company.name.asc(),
    "updated": Company.updated_at.desc(),
    "created": Company.created_at.desc(),
    "city": Company.city.asc(),
}


def _enum(cls, value: str | None):
    if not value:
        return None
    try:
        return cls(value)
    except ValueError:
        return None


def _int(value: str | int | None, default: int | None = None) -> int | None:
    """Form/sorgu alanından hoşgörülü tam sayı: boş ya da bozuk değer -> varsayılan (422 yerine)."""
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def _filtered(session: Session, *, q, city, status, review, size, source, min_score, sort):
    stmt = select(Company)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Company.name.ilike(like), Company.domain.ilike(like), Company.sector.ilike(like), Company.notes.ilike(like)))
    if city:
        stmt = stmt.where(Company.city == (normalize_city(city) or city))
    st = _enum(AppStatus, status)
    if st:
        stmt = stmt.where(Company.status == st)
    rv = _enum(ReviewStatus, review)
    if rv:
        stmt = stmt.where(Company.review_status == rv)
    sb = _enum(SizeBucket, size)
    if sb:
        stmt = stmt.where(Company.size_bucket == sb)
    if source:
        stmt = stmt.where(Company.sources.any(CompanySource.source == source))
    if min_score:
        stmt = stmt.where(Company.score >= min_score)
    stmt = stmt.order_by(SORTS.get(sort or "score", SORTS["score"]), Company.id.asc())
    return stmt


def _filter_options(session: Session) -> dict:
    cities = [c for (c,) in session.execute(select(Company.city).distinct().order_by(Company.city)).all() if c]
    sources = [s for (s,) in session.execute(select(CompanySource.source).distinct().order_by(CompanySource.source)).all()]
    return {"cities": cities, "sources": sources}


@router.get("/sirketler")
def list_companies(
    request: Request,
    q: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    review: Optional[str] = None,
    size: Optional[str] = None,
    source: Optional[str] = None,
    min_score: Optional[str] = None,
    sort: Optional[str] = "score",
    page: Optional[str] = None,
    session: Session = Depends(get_db),
):
    from ..app import render

    score_min = _int(min_score)
    sort = sort if sort in SORTS else "score"
    stmt = _filtered(session, q=q, city=city, status=status, review=review, size=size, source=source, min_score=score_min, sort=sort)
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page_no = max(1, _int(page, 1) or 1)
    rows = list(session.scalars(stmt.options(selectinload(Company.contacts), selectinload(Company.sources)).offset((page_no - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    params = {k: v for k, v in request.query_params.items() if k not in ("page", "mesaj") and v}
    return render(
        request, "companies.html", companies=rows, total=total, page=page_no, pages=pages, params=params,
        filters=dict(q=q or "", city=city or "", status=status or "", review=review or "", size=size or "", source=source or "", min_score="" if score_min is None else score_min, sort=sort),
        **_filter_options(session),
    )


@router.get("/sirketler.csv")
def export_csv(
    q: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    review: Optional[str] = None,
    size: Optional[str] = None,
    source: Optional[str] = None,
    min_score: Optional[str] = None,
    sort: Optional[str] = "score",
    session: Session = Depends(get_db),
):
    from ..app import REVIEW_LABELS

    stmt = _filtered(session, q=q, city=city, status=status, review=review, size=size, source=source, min_score=_int(min_score), sort=sort if sort in SORTS else "score")
    stmt = stmt.options(selectinload(Company.contacts), selectinload(Company.sources))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "ad", "sehir", "ilce", "web", "e-posta", "sektor", "puan", "inceleme", "durum", "kaynaklar", "teknolojiler", "etiketler", "arge", "teknopark", "okul_protokol", "guncelleme"])
    for c in session.scalars(stmt):
        pc = c.primary_contact
        w.writerow([
            c.id, c.name, c.city or "", c.district or "", c.website or "", pc.email if pc else "", c.sector or "", c.score,
            REVIEW_LABELS[c.review_status], APP_STATUS_LABELS[c.status], ",".join(c.source_names), ",".join(c.tech_stack or []), ",".join(c.tags or []),
            "evet" if c.is_arge_center else "", "evet" if c.is_teknopark else "", "evet" if c.school_protocol else "",
            c.updated_at.strftime("%Y-%m-%d") if c.updated_at else "",
        ])
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="sirketler.csv"'})


def _get_company(session: Session, company_id: int) -> Company:
    c = session.get(Company, company_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Şirket bulunamadı")
    return c


@router.get("/sirketler/{company_id}")
def company_detail(request: Request, company_id: int, session: Session = Depends(get_db)):
    from ..app import render
    from ...enrich import scoring

    c = _get_company(session, company_id)
    try:
        explanation = scoring.explain(c)
    except Exception as e:  # noqa: BLE001
        log.warning("Puan açıklaması üretilemedi [%s]: %s", c.id, e)
        explanation = []
    return render(request, "company_detail.html", c=c, explanation=explanation, statuses=list(AppStatus), kinds=list(ContactKind))


@router.post("/sirketler/{company_id}/durum")
def change_status(company_id: int, status: str = Form(...), note: str = Form(""), session: Session = Depends(get_db)):
    from ..app import redirect

    c = _get_company(session, company_id)
    new = _enum(AppStatus, status)
    if new is None:
        raise HTTPException(status_code=400, detail="Geçersiz durum")
    text = clean_text(note) or None
    old = c.status
    if new == old:
        # Aynı duruma "geçiş" zaman çizelgesini kirletmesin; not varsa yalnızca not yazılır.
        if text:
            services.add_note(session, c, text)
            return redirect(f"/sirketler/{c.id}", f"Durum zaten {APP_STATUS_LABELS[new]}; not eklendi")
        return redirect(f"/sirketler/{c.id}", f"Durum değişmedi (zaten {APP_STATUS_LABELS[new]})")
    services.change_status(session, c, new, actor=Actor.user, body=text)
    msg = f"Durum güncellendi: {APP_STATUS_LABELS[old]} → {APP_STATUS_LABELS[new]}"
    if not services.is_system_transition_allowed(old, new):
        msg += " (Uyarı: sistem akışı dışında elle geçiş; zaman çizelgesine kullanıcı işlemi olarak yazıldı)"
    return redirect(f"/sirketler/{c.id}", msg)


@router.post("/sirketler/{company_id}/not")
def add_note(company_id: int, body: str = Form(...), session: Session = Depends(get_db)):
    from ..app import redirect

    c = _get_company(session, company_id)
    text = clean_text(body)
    if text:
        services.add_note(session, c, text)
    return redirect(f"/sirketler/{c.id}", "Not eklendi" if text else "Boş not eklenmedi")


@router.post("/sirketler/{company_id}/kisi")
def add_contact(company_id: int, email: str = Form(...), name: str = Form(""), role: str = Form(""), kind: str = Form(""), make_primary: bool = Form(False), session: Session = Depends(get_db)):
    from ..app import redirect

    c = _get_company(session, company_id)
    contact = services.add_contact(session, c, email, name=clean_text(name), role=clean_text(role), kind=_enum(ContactKind, kind), source="manual", make_primary=True if make_primary else None)
    if contact is None:
        return redirect(f"/sirketler/{c.id}", "Geçersiz e-posta adresi")
    return redirect(f"/sirketler/{c.id}", f"Kişi eklendi: {contact.email}")


@router.post("/sirketler/{company_id}/kisi/{contact_id}/birincil")
def set_primary(company_id: int, contact_id: int, session: Session = Depends(get_db)):
    from ..app import redirect

    c = _get_company(session, company_id)
    contact = session.get(Contact, contact_id)
    if contact is None or contact.company_id != c.id:
        raise HTTPException(status_code=404, detail="Kişi bulunamadı")
    services.set_primary_contact(session, c, contact)
    return redirect(f"/sirketler/{c.id}", f"Birincil kişi: {contact.email}")


@router.post("/sirketler/{company_id}/kisi/{contact_id}/sil")
def delete_contact(company_id: int, contact_id: int, session: Session = Depends(get_db)):
    from ..app import redirect

    c = _get_company(session, company_id)
    contact = session.get(Contact, contact_id)
    if contact is None or contact.company_id != c.id:
        raise HTTPException(status_code=404, detail="Kişi bulunamadı")
    was_primary = contact.is_primary
    session.delete(contact)
    session.flush()
    session.refresh(c)
    msg = "Kişi silindi"
    if was_primary and c.contacts:
        services.set_primary_contact(session, c, min(c.contacts, key=lambda k: k.priority))
    elif not c.contacts and c.status == AppStatus.ready:
        # "Hazır" = onaylı + e-posta var; e-posta kalmadıysa geri "Bulundu".
        services.change_status(session, c, AppStatus.found, actor=Actor.user, title="Bulundu: e-posta kalmadı (kişi silindi)")
        msg += "; e-posta kalmadığı için durum Bulundu'ya döndü"
    return redirect(f"/sirketler/{c.id}", msg)


@router.post("/sirketler/{company_id}/alanlar")
def edit_fields(
    company_id: int,
    website: str = Form(""),
    employee_count: str = Form(""),
    linkedin_url: str = Form(""),
    career_url: str = Form(""),
    sector: str = Form(""),
    priority: str = Form("3"),
    school_protocol: bool = Form(False),
    is_arge_center: bool = Form(False),
    is_teknopark: bool = Form(False),
    has_internship_posting: bool = Form(False),
    notes: str = Form(""),
    session: Session = Depends(get_db),
):
    from ..app import redirect

    c = _get_company(session, company_id)
    warnings: list[str] = []
    if website.strip():
        w = normalize_url(website)
        if w:
            c.website = w[:512]
            dom = domain_of(w)
            if dom and not c.domain:
                taken = session.scalar(select(Company.id).where(Company.domain == dom, Company.id != c.id))
                if taken is None:
                    c.domain = dom
        else:
            warnings.append("web sitesi geçersiz, değiştirilmedi")
    ec = employee_count.strip()
    if ec.isdigit():
        c.employee_count = int(ec)
        c.size_bucket = size_bucket_for(int(ec))
    elif ec:
        warnings.append("çalışan sayısı sayı değil, değiştirilmedi")
    # Bağlantılar yalnızca http(s) olabilir: "javascript:" vb. şemalar href'e girmesin.
    for attr, raw, label in (("linkedin_url", linkedin_url, "LinkedIn"), ("career_url", career_url, "kariyer sayfası")):
        if not raw.strip():
            setattr(c, attr, None)
            continue
        u = normalize_url(clean_text(raw, 512))
        if u:
            setattr(c, attr, u[:512])
        else:
            warnings.append(f"{label} adresi geçersiz, değiştirilmedi")
    c.sector = clean_text(sector, 128)
    c.priority = max(1, min(5, _int(priority, c.priority) or c.priority))
    c.school_protocol = bool(school_protocol)
    c.is_arge_center = bool(is_arge_center)
    c.is_teknopark = bool(is_teknopark)
    c.has_internship_posting = bool(has_internship_posting)
    # Notlarda satır sonları korunur (clean_text hepsini tek boşluğa indirir).
    note_text = notes.replace("\r\n", "\n").strip()
    c.notes = note_text[:5000] if note_text else None
    c.updated_at = now_utc()
    services.add_event(session, c, services.EventType.manual, "Alanlar elle düzenlendi", actor=Actor.user)
    msg = "Alanlar kaydedildi" + (" (" + "; ".join(warnings) + ")" if warnings else "")
    return redirect(f"/sirketler/{c.id}", msg)


@router.post("/sirketler/{company_id}/karaliste")
def blacklist(company_id: int, flag: bool = Form(True), session: Session = Depends(get_db)):
    from ..app import redirect

    c = _get_company(session, company_id)
    services.set_blacklist(session, c, bool(flag))
    return redirect(f"/sirketler/{c.id}", "Kara listeye alındı" if flag else "Kara listeden çıkarıldı")


@router.post("/sirketler/{company_id}/inceleme")
def review_action(company_id: int, action: str = Form(...), session: Session = Depends(get_db)):
    from ..app import redirect

    c = _get_company(session, company_id)
    if action == "approve":
        services.approve_company(session, c)
        msg = "Onaylandı"
    elif action == "reject":
        services.reject_company(session, c)
        msg = "Reddedildi"
    elif action == "later":
        services.defer_company(session, c)
        msg = "Sonraya bırakıldı"
    else:
        raise HTTPException(status_code=400, detail="Geçersiz işlem")
    return redirect(f"/sirketler/{c.id}", msg)


@router.post("/sirketler/{company_id}/zenginlestir")
def enrich_now(company_id: int, session: Session = Depends(get_db)):
    from ..app import redirect
    from ...enrich import website as enrich_website
    from ...enrich import scoring

    c = _get_company(session, company_id)
    res = enrich_website.enrich_company(c, session)
    c.score = scoring.compute_score(c)
    return redirect(f"/sirketler/{c.id}", res.summary())


@router.post("/sirketler/{company_id}/puanla")
def rescore(company_id: int, session: Session = Depends(get_db)):
    from ..app import redirect
    from ...enrich import scoring

    c = _get_company(session, company_id)
    c.score = scoring.compute_score(c)
    return redirect(f"/sirketler/{c.id}", f"Puan: {c.score}")


@router.post("/sirketler/{company_id}/taslak")
def compose_now(company_id: int, use_llm: bool = Form(False), session: Session = Depends(get_db)):
    from ..app import redirect
    from ...mail import compose

    c = _get_company(session, company_id)
    res = compose.compose_for_company(c, session, use_llm=bool(use_llm))
    if not res.ok:
        return redirect(f"/sirketler/{c.id}", f"Taslak üretilemedi: {res.error}")
    return redirect(f"/mailler?company={c.id}", "Taslak üretildi; önizleyip onaylayın")
