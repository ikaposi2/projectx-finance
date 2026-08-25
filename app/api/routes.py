from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CompensationRow,
    InvoiceCreate,
    InvoiceOut,
    InvoiceUpdate,
    ReserveSnapshot,
)
from app.auth.jwt import Principal, decode_access_token
from app.core.config import get_settings
from app.db.session import get_db
from app.services import ledger

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


def _to_invoice(row) -> InvoiceOut:
    return InvoiceOut(
        id=row.id,
        project_id=row.project_id,
        customer_id=row.customer_id,
        customer_name=row.customer_name,
        amount_eur=row.amount_eur,
        status=row.status,
        notes=row.notes,
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@router.get("/compensation", response_model=list[CompensationRow])
async def get_compensation(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[CompensationRow]:
    _require_manager(principal)
    rows = await ledger.list_compensation(db, tenant_id=principal.tenant_id)
    return [CompensationRow(**r) for r in rows]


@router.get("/reserve", response_model=ReserveSnapshot)
async def get_reserve(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> ReserveSnapshot:
    _require_manager(principal)
    snap = await ledger.reserve_snapshot(db, tenant_id=principal.tenant_id)
    return ReserveSnapshot(**snap)


@router.get("/invoices", response_model=list[InvoiceOut])
async def get_invoices(
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceOut]:
    _require_manager(principal)
    rows = await ledger.list_invoices(db, tenant_id=principal.tenant_id)
    return [_to_invoice(r) for r in rows]


@router.post("/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
async def post_invoice(
    body: InvoiceCreate,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    _require_manager(principal)
    row = await ledger.create_invoice(
        db,
        tenant_id=principal.tenant_id,
        project_id=body.project_id,
        customer_id=body.customer_id,
        customer_name=body.customer_name,
        amount_eur=body.amount_eur,
        notes=body.notes,
    )
    return _to_invoice(row)


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
    try:
        row = await ledger.update_invoice_status(db, row, status=body.status)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_invoice(row)
