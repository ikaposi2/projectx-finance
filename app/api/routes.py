from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
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
    ReserveSnapshot,
    VatAccountOut,
    VatRemitRequest,
)
from app.auth.jwt import Principal, decode_access_token
from app.core.config import get_settings
from app.db.session import get_db
from app.services import ledger
from app.services.billing import (
    BillingError,
    generate_invoice,
    get_or_create_company,
    list_billing_candidates,
    list_invoice_lines,
    update_company,
)
from app.services.clients import UpstreamError, fetch_bookable_projects, fetch_user_names, refuse_time_entry
from app.services.pdf import resolve_pdf_absolute

router = APIRouter(tags=["finance"])
security = HTTPBearer(auto_error=False)
settings = get_settings()
MANAGER_ROLES = {"partner", "manager", "admin"}


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> Principal:
    if creds is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    try:
        return decode_access_token(creds.credentials)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid_token") from None


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
    out: list[CompensationEffectOut] = []
    for row in effects:
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
                can_undo=row.time_entry_id not in invoiced_ids,
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


@router.get("/billing/candidates", response_model=list[BillingCandidate])
async def get_billing_candidates(
    principal: Principal = Depends(current_principal),
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> list[BillingCandidate]:
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
        )
    except UpstreamError as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    return [BillingCandidate(**r) for r in rows]


@router.get("/invoices", response_model=list[InvoiceOut])
async def get_invoices(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceOut]:
    _require_manager(principal)
    rows = await ledger.list_invoices(db, tenant_id=principal.tenant_id)
    return [await _to_invoice(db, r) for r in rows]


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
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except UpstreamError as exc:
        code = 404 if "not_found" in exc.detail else 503
        raise HTTPException(status_code=code, detail=exc.detail) from exc
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
    path = resolve_pdf_absolute(row.pdf_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="pdf_missing")
    filename = f"{row.invoice_number or invoice_id}.pdf"
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
async def patch_invoice(
    invoice_id: str,
    body: InvoiceUpdate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    _require_manager(principal)
    row = await ledger.get_invoice(db, tenant_id=principal.tenant_id, invoice_id=invoice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    lines = await list_invoice_lines(db, row.id) if body.status == "issued" else None
    try:
        row = await ledger.update_invoice_status(db, row, status=body.status, lines=lines)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
