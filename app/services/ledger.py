from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import CompensationEffect, Invoice

settings = get_settings()


async def apply_time_approval(
    db: AsyncSession,
    *,
    tenant_id: str,
    time_entry_id: str,
    partner_id: str,
    project_id: str | None,
    hours: float,
    classification: str,
) -> bool:
    existing = await db.get(CompensationEffect, time_entry_id)
    if existing is not None and existing.applied:
        return False

    rate = float(settings.internal_rate_eur) if classification == "approved_non_billable" else 0.0
    amount = round(float(hours) * rate, 2)

    if existing is None:
        existing = CompensationEffect(
            time_entry_id=time_entry_id,
            tenant_id=tenant_id,
            partner_id=partner_id,
            project_id=project_id,
            classification=classification,
            hours=float(hours),
            rate_eur=rate,
            amount_eur=amount,
            applied=True,
        )
        db.add(existing)
    else:
        existing.tenant_id = tenant_id
        existing.partner_id = partner_id
        existing.project_id = project_id
        existing.classification = classification
        existing.hours = float(hours)
        existing.rate_eur = rate
        existing.amount_eur = amount
        existing.applied = True

    await db.commit()
    return True


async def reverse_time_effect(db: AsyncSession, *, time_entry_id: str) -> bool:
    existing = await db.get(CompensationEffect, time_entry_id)
    if existing is None or not existing.applied:
        return False
    existing.applied = False
    await db.commit()
    return True


async def list_compensation(db: AsyncSession, *, tenant_id: str) -> list[dict]:
    result = await db.scalars(
        select(CompensationEffect).where(
            CompensationEffect.tenant_id == tenant_id,
            CompensationEffect.applied.is_(True),
        )
    )
    by_partner: dict[str, dict] = {}
    for row in result:
        bucket = by_partner.setdefault(
            row.partner_id,
            {
                "partner_id": row.partner_id,
                "billable_hours": 0.0,
                "chargeback_hours": 0.0,
                "chargeback_eur": 0.0,
            },
        )
        if row.classification == "billable":
            bucket["billable_hours"] = round(bucket["billable_hours"] + float(row.hours), 2)
        elif row.classification == "approved_non_billable":
            bucket["chargeback_hours"] = round(bucket["chargeback_hours"] + float(row.hours), 2)
            bucket["chargeback_eur"] = round(bucket["chargeback_eur"] + float(row.amount_eur), 2)
    return sorted(by_partner.values(), key=lambda r: r["partner_id"])


async def reserve_snapshot(db: AsyncSession, *, tenant_id: str) -> dict:
    effects = await db.scalars(
        select(CompensationEffect).where(
            CompensationEffect.tenant_id == tenant_id,
            CompensationEffect.applied.is_(True),
        )
    )
    chargeback_eur = 0.0
    billable_hours = 0.0
    chargeback_hours = 0.0
    for row in effects:
        if row.classification == "approved_non_billable":
            chargeback_eur += float(row.amount_eur)
            chargeback_hours += float(row.hours)
        elif row.classification == "billable":
            billable_hours += float(row.hours)

    invoices = await db.scalars(select(Invoice).where(Invoice.tenant_id == tenant_id))
    issued_eur = 0.0
    paid_eur = 0.0
    draft_eur = 0.0
    for inv in invoices:
        if inv.status == "issued":
            issued_eur += float(inv.amount_eur)
        elif inv.status == "paid":
            paid_eur += float(inv.amount_eur)
        elif inv.status == "draft":
            draft_eur += float(inv.amount_eur)

    revenue_eur = issued_eur + paid_eur
    # Approximate internal P&L proxy until full cost model exists.
    current_reserve_eur = round(revenue_eur - chargeback_eur, 2)
    target = float(settings.reserve_target_eur)
    surplus = round(max(0.0, current_reserve_eur - target), 2)
    return {
        "target_eur": target,
        "current_reserve_eur": current_reserve_eur,
        "surplus_eur": surplus,
        "internal_rate_eur": float(settings.internal_rate_eur),
        "chargeback_hours": round(chargeback_hours, 2),
        "chargeback_eur": round(chargeback_eur, 2),
        "billable_hours": round(billable_hours, 2),
        "invoice_draft_eur": round(draft_eur, 2),
        "invoice_issued_eur": round(issued_eur, 2),
        "invoice_paid_eur": round(paid_eur, 2),
    }


async def list_invoices(db: AsyncSession, *, tenant_id: str) -> list[Invoice]:
    result = await db.scalars(
        select(Invoice).where(Invoice.tenant_id == tenant_id).order_by(Invoice.created_at.desc())
    )
    return list(result)


async def create_invoice(
    db: AsyncSession,
    *,
    tenant_id: str,
    project_id: str | None,
    customer_id: str | None,
    customer_name: str,
    amount_eur: float,
    notes: str | None,
) -> Invoice:
    row = Invoice(
        tenant_id=tenant_id,
        project_id=project_id,
        customer_id=customer_id,
        customer_name=customer_name.strip(),
        amount_eur=float(amount_eur),
        status="draft",
        notes=notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_invoice(db: AsyncSession, *, tenant_id: str, invoice_id: str) -> Invoice | None:
    row = await db.get(Invoice, invoice_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


async def update_invoice_status(db: AsyncSession, row: Invoice, *, status: str) -> Invoice:
    allowed = {"draft", "issued", "paid"}
    if status not in allowed:
        raise ValueError("invalid_status")
    transitions = {
        "draft": {"issued"},
        "issued": {"paid", "draft"},
        "paid": {"issued"},
    }
    if status != row.status and status not in transitions.get(row.status, set()):
        raise ValueError("invalid_transition")
    row.status = status
    await db.commit()
    await db.refresh(row)
    return row
