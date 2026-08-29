from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    BillingCandidate,
    CompanyProfileOut,
    CompanyProfileUpdate,
    CompensationEffectOut,
    InvoiceAgendaItem,
    InvoiceGenerate,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceUpdate,
    MonthlyCostCreate,
    MonthlyCostOut,
    MonthlyCostUpdate,
    PersonnelCandidateOut,
    PersonnelGenerate,
    InboxMessageOut,
    InboxUnreadOut,
    InboxOpenOut,
    MonthlyPersonnelRunOut,
    ReportSummaryOut,
    ReserveSnapshot,
    VatAccountOut,
    VatRemitRequest,
)
from app.auth.jwt import Principal, decode_access_token
from app.auth.service_token import mint_service_access_token
from app.core.config import get_settings
from app.db.session import get_db
from app.observability import audit
from app.services import costs as cost_service
from app.services import ledger
from app.services import personnel as personnel_service
from app.services import inbox as inbox_service
from app.services.billing import (
    BillingError,
    generate_invoice,
    get_or_create_company,
    list_billing_candidates,
    list_invoice_lines,
    update_company,
)
from app.services import reports as reports_service
from app.services.clients import (
    UpstreamError,
    advance_project_funnel,
    fetch_bookable_projects,
    fetch_project,
    fetch_user_names,
    refuse_time_entry,
)
from app.services.costs import CostError

router = APIRouter(tags=["finance"])
security = HTTPBearer(auto_error=False)
settings = get_settings()
MANAGER_ROLES = {"partner", "manager", "admin"}
SETTLED_FUNNEL = frozenset({"closed", "paid"})


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> Principal:
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        return decode_access_token(creds.credentials)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_token") from None


async def _closed_project_ids(*, access_token: str, project_ids: set[str]) -> set[str]:
    """Project ids whose funnel is settled (closed/paid) — compensation undo blocked."""
    closed: set[str] = set()
    for pid in project_ids:
        if not pid:
            continue
        try:
            project = await fetch_project(project_id=pid, access_token=access_token)
        except UpstreamError:
            continue
        status = str(project.get("funnel_status") or "").strip()
        if status == "finalizing":
            status = "delivered"
        if status in SETTLED_FUNNEL:
            closed.add(pid)
    return closed


async def _project_is_closed(*, access_token: str, project_id: str | None) -> bool:
    if not project_id:
        return False
    return project_id in await _closed_project_ids(
        access_token=access_token, project_ids={project_id}
    )


def _require_manager(principal: Principal) -> None:
    if principal.role not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="not_manager")


async def _to_invoice(db: AsyncSession, row) -> InvoiceOut:
    lines = await list_invoice_lines(db, row.id)
    return InvoiceOut(
        id=row.id,
        invoice_number=row.invoice_number or row.id[:8],
        kind=row.kind or "manual",
        project_id=row.project_id,
        project_name=row.project_name or "",
        customer_id=row.customer_id,
        customer_name=row.customer_name,
        partner_id=getattr(row, "partner_id", None),
        buyer_vat_id=getattr(row, "buyer_vat_id", None),
        buyer_address=getattr(row, "buyer_address", None),
        seller_name=getattr(row, "seller_name", "") or "",
        seller_vat_id=getattr(row, "seller_vat_id", None),
        seller_address=getattr(row, "seller_address", None),
        seller_bank_account=getattr(row, "seller_bank_account", None),
        description=getattr(row, "description", None),
        period_label=getattr(row, "period_label", None),
        subtotal_eur=float(getattr(row, "subtotal_eur", None) or row.amount_eur or 0),
        vat_rate=float(getattr(row, "vat_rate", None) or 21),
        vat_eur=float(getattr(row, "vat_eur", None) or 0),
        amount_eur=float(row.amount_eur or 0),
        payment_terms_days=int(getattr(row, "payment_terms_days", None) or 30),
        issued_at=row.issued_at.isoformat() if getattr(row, "issued_at", None) else None,
        due_date=row.due_date.isoformat() if getattr(row, "due_date", None) else None,
        returned_at=row.returned_at.isoformat() if getattr(row, "returned_at", None) else None,
        pdf_path=getattr(row, "pdf_path", None),
        status=row.status,
        notes=row.notes,
        lines=[
            InvoiceLineOut(
                id=line.id,
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price_eur=line.unit_price_eur,
                amount_eur=line.amount_eur,
                source=line.source,
                time_entry_id=line.time_entry_id,
            )
            for line in lines
        ],
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@router.get("/compensation", response_model=list[CompensationEffectOut])
async def get_compensation(
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> list[CompensationEffectOut]:
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    names = await fetch_user_names(access_token=creds.credentials)
    effects = await ledger.list_compensation_effects(db, tenant_id=principal.tenant_id)
    invoiced_ids = await ledger.invoiced_time_entry_ids(db, tenant_id=principal.tenant_id)
    project_ids = {str(row.project_id) for row in effects if row.project_id}
    closed_ids = await _closed_project_ids(access_token=creds.credentials, project_ids=project_ids)
    out: list[CompensationEffectOut] = []
    for row in effects:
        blocked: str | None = None
        if row.time_entry_id in invoiced_ids:
            blocked = "already_invoiced"
        elif row.project_id and str(row.project_id) in closed_ids:
            blocked = "project_closed"
        out.append(
            CompensationEffectOut(
                time_entry_id=row.time_entry_id,
                partner_id=row.partner_id,
                partner_name=names.get(row.partner_id) or row.partner_id,
                project_id=row.project_id,
                classification=row.classification,
                hours=float(row.hours),
                rate_eur=float(row.rate_eur),
                amount_eur=float(row.amount_eur),
                can_undo=blocked is None,
                undo_blocked_reason=blocked,
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
            )
        )
    return out


@router.post("/compensation/{time_entry_id}/undo")
async def undo_compensation(
    time_entry_id: str,
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Undo a ledger effect by refusing the related time entry (reopens hours)."""
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    effect = await ledger.get_applied_effect(
        db, tenant_id=principal.tenant_id, time_entry_id=time_entry_id
    )
    if effect is None:
        raise HTTPException(status_code=404, detail="effect_not_found")
    if await ledger.effect_is_invoiced(db, tenant_id=principal.tenant_id, time_entry_id=time_entry_id):
        raise HTTPException(status_code=409, detail="already_invoiced")
    if await _project_is_closed(access_token=creds.credentials, project_id=effect.project_id):
        raise HTTPException(status_code=409, detail="project_closed")
    try:
        await refuse_time_entry(time_entry_id=time_entry_id, access_token=creds.credentials)
    except UpstreamError as exc:
        status_code = 409 if exc.detail == "not_refusable" else 503
        if exc.detail == "time_entry_not_found":
            status_code = 404
        raise HTTPException(status_code=status_code, detail=exc.detail) from exc
    # Reverse immediately; refuse event will also reverse (idempotent).
    await ledger.reverse_time_effect(db, time_entry_id=time_entry_id)
    return {"status": "undone", "time_entry_id": time_entry_id}


@router.get("/reserve", response_model=ReserveSnapshot)
async def get_reserve(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> ReserveSnapshot:
    _require_manager(principal)
    snap = await ledger.reserve_snapshot(db, tenant_id=principal.tenant_id)
    return ReserveSnapshot(**snap)


@router.get("/vat", response_model=VatAccountOut)
async def get_vat_account(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> VatAccountOut:
    _require_manager(principal)
    snap = await ledger.vat_account_snapshot(db, tenant_id=principal.tenant_id)
    return VatAccountOut(**snap)


@router.post("/vat/remit", response_model=VatAccountOut)
async def post_vat_remit(
    body: VatRemitRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> VatAccountOut:
    """Record a quarterly VAT payment (money leaves the VAT account, not the reserve)."""
    _require_manager(principal)
    try:
        await ledger.record_vat_remittance(
            db,
            tenant_id=principal.tenant_id,
            year=body.year,
            quarter=body.quarter,
            amount_eur=body.amount_eur,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    snap = await ledger.vat_account_snapshot(db, tenant_id=principal.tenant_id)
    return VatAccountOut(**snap)


@router.get("/company", response_model=CompanyProfileOut)
async def get_company(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> CompanyProfileOut:
    _require_manager(principal)
    row = await get_or_create_company(db, principal.tenant_id)
    return CompanyProfileOut(
        legal_name=row.legal_name,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        postal_code=row.postal_code,
        city=row.city,
        country=row.country,
        vat_id=row.vat_id,
        coc_number=row.coc_number,
        bank_account=row.bank_account,
        invoice_email=row.invoice_email,
        payment_terms_days=row.payment_terms_days,
    )


@router.patch("/company", response_model=CompanyProfileOut)
async def patch_company(
    body: CompanyProfileUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> CompanyProfileOut:
    _require_manager(principal)
    row = await get_or_create_company(db, principal.tenant_id)
    row = await update_company(db, row, body.model_dump(exclude_unset=True))
    return CompanyProfileOut(
        legal_name=row.legal_name,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        postal_code=row.postal_code,
        city=row.city,
        country=row.country,
        vat_id=row.vat_id,
        coc_number=row.coc_number,
        bank_account=row.bank_account,
        invoice_email=row.invoice_email,
        payment_terms_days=row.payment_terms_days,
    )


@router.get("/reports/summary", response_model=ReportSummaryOut)
async def get_report_summary(
    from_date: date = Query(..., alias="from", description="Period start (inclusive)"),
    to_date: date = Query(..., alias="to", description="Period end (inclusive)"),
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> ReportSummaryOut:
    """Five management figures: funnel, in-progress WIP, utilization, delivered, received."""
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        snap = await reports_service.report_summary(
            db,
            tenant_id=principal.tenant_id,
            access_token=creds.credentials,
            from_day=from_date,
            to_day=to_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc) or "invalid_range") from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    return ReportSummaryOut(**snap)


@router.get("/billing/candidates", response_model=list[BillingCandidate])
async def get_billing_candidates(
    month: str | None = Query(
        default=None,
        min_length=7,
        max_length=7,
        pattern=r"^\d{4}-\d{2}$",
        description="Billing month YYYY-MM for T&M hour candidates (default: current UTC month)",
    ),
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> list[BillingCandidate]:
    """Projects ready to invoice; T&M actions scoped to the selected month."""
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        projects = await fetch_bookable_projects(access_token=creds.credentials, include_complete=True)
        rows = await list_billing_candidates(
            db,
            tenant_id=principal.tenant_id,
            access_token=creds.credentials,
            projects=projects,
            period_label=month,
        )
    except BillingError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    return [BillingCandidate(**r) for r in rows]


@router.get("/invoices", response_model=list[InvoiceOut])
async def get_invoices(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceOut]:
    _require_manager(principal)
    rows = await ledger.list_invoices(db, tenant_id=principal.tenant_id, include_personnel=False)
    return [await _to_invoice(db, r) for r in rows]


@router.get("/personnel-invoices/candidates", response_model=list[PersonnelCandidateOut])
async def get_personnel_candidates(
    month: str = Query(min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> list[PersonnelCandidateOut]:
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        rows = await personnel_service.personnel_candidates(
            db,
            tenant_id=principal.tenant_id,
            access_token=creds.credentials,
            month=month,
        )
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    return [PersonnelCandidateOut(**r) for r in rows]


@router.get("/personnel-invoices", response_model=list[InvoiceOut])
async def get_personnel_invoices(
    month: str | None = Query(default=None, min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceOut]:
    _require_manager(principal)
    rows = await personnel_service.list_personnel_proposals(
        db, tenant_id=principal.tenant_id, month=month
    )
    return [await _to_invoice(db, r) for r in rows]


@router.post(
    "/personnel-invoices/generate",
    response_model=InvoiceOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_personnel_generate(
    body: PersonnelGenerate,
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        row = await personnel_service.generate_personnel_proposal(
            db,
            tenant_id=principal.tenant_id,
            access_token=creds.credentials,
            partner_id=body.partner_id,
            month=body.month,
        )
    except BillingError as exc:
        code = 409 if exc.detail in {"no_hours_for_month"} else 422
        raise HTTPException(status_code=code, detail=exc.detail) from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.detail) from exc
    return await _to_invoice(db, row)


def _verify_internal_cron(x_internal_token: str | None = Header(default=None)) -> None:
    expected = (settings.internal_cron_token or "").strip()
    if not expected or not x_internal_token or x_internal_token != expected:
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/internal/personnel-invoices/run-monthly", response_model=MonthlyPersonnelRunOut)
async def run_monthly_personnel_proposals(
    month: str | None = Query(default=None, min_length=7, max_length=7, pattern=r"^\d{4}-\d{2}$"),
    _: None = Depends(_verify_internal_cron),
    db: AsyncSession = Depends(get_db),
) -> MonthlyPersonnelRunOut:
    tenant_id = (settings.cron_tenant_id or "").strip()
    actor_id = (settings.cron_actor_user_id or "").strip()
    if not tenant_id or not actor_id:
        raise HTTPException(status_code=503, detail="cron_not_configured")
    target_month = month or personnel_service.previous_calendar_month()
    token = mint_service_access_token(user_id=actor_id, tenant_id=tenant_id, role="admin")
    stats = await personnel_service.generate_monthly_personnel_proposals(
        db,
        tenant_id=tenant_id,
        access_token=token,
        month=target_month,
    )
    return MonthlyPersonnelRunOut(**stats)


@router.get("/inbox", response_model=list[InboxMessageOut])
async def get_inbox(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[InboxMessageOut]:
    rows = await inbox_service.list_inbox(
        db, tenant_id=principal.tenant_id, user_id=principal.user_id
    )
    return [InboxMessageOut(**row) for row in rows]


@router.get("/inbox/unread-count", response_model=InboxUnreadOut)
async def get_inbox_unread_count(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> InboxUnreadOut:
    count = await inbox_service.unread_inbox_count(
        db, tenant_id=principal.tenant_id, user_id=principal.user_id
    )
    return InboxUnreadOut(count=count)


@router.post("/inbox/{message_id}/open", response_model=InboxOpenOut)
async def open_inbox_message(
    message_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> InboxOpenOut:
    result = await inbox_service.open_inbox_message(
        db,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        message_id=message_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="not_found")
    return InboxOpenOut(**result)


@router.get("/inbox/invoices/{invoice_id}/pdf")
async def get_partner_invoice_pdf(
    invoice_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    row = await inbox_service.get_partner_invoice_pdf(
        db,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        invoice_id=invoice_id,
    )
    if row is None or not row.pdf_path:
        raise HTTPException(status_code=404, detail="not_found")
    from app.services.pdf import load_pdf_bytes

    data = load_pdf_bytes(row.pdf_path)
    if not data:
        raise HTTPException(status_code=404, detail="pdf_missing")
    filename = f"{row.invoice_number or invoice_id}.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/invoices/generate", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
async def post_generate_invoice(
    body: InvoiceGenerate,
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        row = await generate_invoice(
            db,
            tenant_id=principal.tenant_id,
            access_token=creds.credentials,
            project_id=body.project_id,
            kind=body.kind,
            description=body.description,
            period_label=body.period_label,
        )
    except BillingError as exc:
        code = 422 if exc.detail in {"invalid_month", "no_hours_for_month", "no_billable_hours"} else 409
        raise HTTPException(status_code=code, detail=exc.detail) from exc
    except UpstreamError as exc:
        code = 404 if "not_found" in exc.detail else 503
        raise HTTPException(status_code=code, detail=exc.detail) from exc
    audit(
        "invoice-generate",
        outcome="success",
        category=["api", "configuration"],
        message="invoice draft generated",
        **{
            "user.id": principal.user_id,
            "user.email": principal.email,
            "organization.id": principal.tenant_id,
            "project.id": body.project_id,
            "invoice.kind": body.kind,
            "invoice.period": body.period_label,
            "invoice.id": row.id,
        },
    )
    return await _to_invoice(db, row)


@router.get("/invoices/agenda", response_model=list[InvoiceAgendaItem])
async def get_invoice_agenda(
    week_start: date = Query(..., description="ISO week start (Monday)"),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceAgendaItem]:
    _require_manager(principal)
    rows = await ledger.invoice_agenda(db, tenant_id=principal.tenant_id, week_start=week_start)
    return [InvoiceAgendaItem(**r) for r in rows]


@router.get("/invoices/{invoice_id}/pdf")
async def get_invoice_pdf(
    invoice_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    _require_manager(principal)
    row = await ledger.get_invoice(db, tenant_id=principal.tenant_id, invoice_id=invoice_id)
    if row is None or not row.pdf_path:
        raise HTTPException(status_code=404, detail="not_found")
    from app.services.pdf import load_pdf_bytes

    data = load_pdf_bytes(row.pdf_path)
    if not data:
        raise HTTPException(status_code=404, detail="pdf_missing")
    filename = f"{row.invoice_number or invoice_id}.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
async def patch_invoice(
    invoice_id: str,
    body: InvoiceUpdate,
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    _require_manager(principal)
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    row = await ledger.get_invoice(db, tenant_id=principal.tenant_id, invoice_id=invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    lines = await list_invoice_lines(db, row.id) if body.status == "issued" else None
    try:
        row = await ledger.update_invoice_status(db, row, status=body.status, lines=lines)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if body.status in {"issued", "paid"}:
        # issued ≈ "sent" today (PDF archived; email delivery not implemented yet)
        action = "invoice-sent" if body.status == "issued" else "invoice-paid"
        audit(
            action,
            outcome="success",
            category=["api", "configuration"],
            message=(
                "invoice marked issued (sent / PDF archived)"
                if body.status == "issued"
                else "invoice marked paid"
            ),
            **{
                "user.id": principal.user_id,
                "user.email": principal.email,
                "organization.id": principal.tenant_id,
                "invoice.id": invoice_id,
                "invoice.status": body.status,
                "project.id": row.project_id,
            },
        )

    # Mirror invoice lifecycle onto the project funnel dial.
    project_id = (row.project_id or "").strip()
    if project_id:
        target: str | None = None
        if body.status == "issued":
            target = "invoiced"
        elif body.status == "paid":
            target = "closed"
        if target:
            try:
                await advance_project_funnel(
                    project_id=project_id,
                    funnel_status=target,
                    access_token=creds.credentials,
                )
            except UpstreamError:
                # Invoice change already committed; funnel sync is best-effort.
                pass

    return await _to_invoice(db, row)


@router.delete("/invoices/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invoice(
    invoice_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_manager(principal)
    row = await ledger.get_invoice(db, tenant_id=principal.tenant_id, invoice_id=invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        await ledger.delete_invoice(db, row)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _to_cost(row) -> MonthlyCostOut:
    return MonthlyCostOut(
        id=row.id,
        label=row.label,
        amount_eur=float(row.amount_eur or 0),
        cadence=row.cadence,
        start_month=row.start_month,
        end_month=row.end_month,
        notes=row.notes,
        invoice_matched=bool(row.invoice_matched),
        invoice_paid=bool(row.invoice_paid),
    )


@router.get("/costs", response_model=list[MonthlyCostOut])
async def get_costs(
    month: str | None = Query(
        default=None,
        min_length=7,
        max_length=7,
        description="YYYY-MM; omit to list all cost definitions",
    ),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[MonthlyCostOut]:
    _require_manager(principal)
    try:
        if month:
            rows = await cost_service.list_costs_for_month(
                db, tenant_id=principal.tenant_id, month=month
            )
        else:
            rows = await cost_service.list_all_costs(db, tenant_id=principal.tenant_id)
    except CostError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return [_to_cost(r) for r in rows]


@router.post("/costs", response_model=MonthlyCostOut, status_code=status.HTTP_201_CREATED)
async def post_cost(
    body: MonthlyCostCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> MonthlyCostOut:
    _require_manager(principal)
    try:
        row = await cost_service.create_cost(
            db,
            tenant_id=principal.tenant_id,
            label=body.label,
            amount_eur=body.amount_eur,
            cadence=body.cadence,
            start_month=body.start_month,
            end_month=body.end_month,
            notes=body.notes,
        )
    except CostError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return _to_cost(row)


@router.patch("/costs/{cost_id}", response_model=MonthlyCostOut)
async def patch_cost(
    cost_id: str,
    body: MonthlyCostUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> MonthlyCostOut:
    _require_manager(principal)
    row = await cost_service.get_cost(db, tenant_id=principal.tenant_id, cost_id=cost_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        row = await cost_service.update_cost(
            db,
            row,
            label=body.label,
            amount_eur=body.amount_eur,
            cadence=body.cadence,
            start_month=body.start_month,
            end_month=body.end_month,
            clear_end_month=body.clear_end_month,
            notes=body.notes,
            invoice_matched=body.invoice_matched,
            invoice_paid=body.invoice_paid,
        )
    except CostError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return _to_cost(row)


@router.delete("/costs/{cost_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cost(
    cost_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> None:
    _require_manager(principal)
    row = await cost_service.get_cost(db, tenant_id=principal.tenant_id, cost_id=cost_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    await cost_service.delete_cost(db, row)
