from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import CompensationEffect, Invoice, InvoiceLine, VatRemittance

settings = get_settings()


def _invoice_net(inv: Invoice) -> float:
    """Revenue excluding VAT (VAT must not enter company reserve)."""
    sub = float(getattr(inv, "subtotal_eur", None) or 0)
    vat = float(getattr(inv, "vat_eur", None) or 0)
    amount = float(inv.amount_eur or 0)
    if sub > 0:
        return sub
    if vat > 0:
        return round(max(0.0, amount - vat), 2)
    return amount


def _invoice_vat(inv: Invoice) -> float:
    return float(getattr(inv, "vat_eur", None) or 0)


def _quarter_key(dt: datetime) -> tuple[int, int]:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.year, (dt.month - 1) // 3 + 1


def _quarter_label(year: int, quarter: int) -> str:
    return f"{year}-Q{quarter}"

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
    """Aggregated totals per partner (legacy summary)."""
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


async def list_compensation_effects(db: AsyncSession, *, tenant_id: str) -> list[CompensationEffect]:
    result = await db.scalars(
        select(CompensationEffect)
        .where(
            CompensationEffect.tenant_id == tenant_id,
            CompensationEffect.applied.is_(True),
        )
        .order_by(CompensationEffect.updated_at.desc())
    )
    return list(result)


async def invoiced_time_entry_ids(db: AsyncSession, *, tenant_id: str) -> set[str]:
    """Hours lock only after invoice is sent (issued) or paid."""
    rows = await db.scalars(
        select(InvoiceLine.time_entry_id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.tenant_id == tenant_id,
            InvoiceLine.time_entry_id.is_not(None),
            Invoice.status.in_(("issued", "paid")),
        )
    )
    return {str(x) for x in rows if x}


async def effect_is_invoiced(db: AsyncSession, *, tenant_id: str, time_entry_id: str) -> bool:
    return time_entry_id in await invoiced_time_entry_ids(db, tenant_id=tenant_id)


async def get_applied_effect(
    db: AsyncSession, *, tenant_id: str, time_entry_id: str
) -> CompensationEffect | None:
    row = await db.get(CompensationEffect, time_entry_id)
    if row is None or row.tenant_id != tenant_id or not row.applied:
        return None
    return row


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
        net = _invoice_net(inv)
        if inv.status == "issued":
            issued_eur += net
        elif inv.status == "paid":
            paid_eur += net
        elif inv.status == "draft":
            draft_eur += net

    # Net revenue only — VAT sits in the separate VAT account.
    revenue_eur = issued_eur + paid_eur
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


async def vat_account_snapshot(db: AsyncSession, *, tenant_id: str) -> dict:
    invoices = await db.scalars(
        select(Invoice).where(
            Invoice.tenant_id == tenant_id,
            Invoice.status.in_(("issued", "paid")),
        )
    )
    collected_by_q: dict[tuple[int, int], float] = {}
    for inv in invoices:
        dt = inv.created_at or datetime.now(timezone.utc)
        key = _quarter_key(dt)
        collected_by_q[key] = round(collected_by_q.get(key, 0.0) + _invoice_vat(inv), 2)

    remits = await db.scalars(
        select(VatRemittance).where(VatRemittance.tenant_id == tenant_id)
    )
    remitted_by_q: dict[tuple[int, int], float] = {}
    for row in remits:
        key = (int(row.year), int(row.quarter))
        remitted_by_q[key] = round(remitted_by_q.get(key, 0.0) + float(row.amount_eur), 2)

    now = datetime.now(timezone.utc)
    cy, cq = _quarter_key(now)
    keys = sorted(set(collected_by_q) | set(remitted_by_q) | {(cy, cq)}, reverse=True)

    quarters: list[dict] = []
    balance = 0.0
    for year, quarter in keys:
        collected = collected_by_q.get((year, quarter), 0.0)
        remitted = remitted_by_q.get((year, quarter), 0.0)
        outstanding = round(max(0.0, collected - remitted), 2)
        balance = round(balance + outstanding, 2)
        quarters.append(
            {
                "year": year,
                "quarter": quarter,
                "label": _quarter_label(year, quarter),
                "collected_eur": collected,
                "remitted_eur": remitted,
                "outstanding_eur": outstanding,
                "can_remit": outstanding > 0.009,
            }
        )

    return {
        "balance_eur": balance,
        "current_quarter": _quarter_label(cy, cq),
        "quarters": quarters,
    }


async def record_vat_remittance(
    db: AsyncSession,
    *,
    tenant_id: str,
    year: int,
    quarter: int,
    amount_eur: float | None,
    notes: str | None,
) -> VatRemittance:
    if quarter not in (1, 2, 3, 4):
        raise ValueError("invalid_quarter")
    snap = await vat_account_snapshot(db, tenant_id=tenant_id)
    qrow = next((q for q in snap["quarters"] if q["year"] == year and q["quarter"] == quarter), None)
    outstanding = float(qrow["outstanding_eur"]) if qrow else 0.0
    pay = float(amount_eur) if amount_eur is not None else outstanding
    if pay <= 0:
        raise ValueError("nothing_to_remit")
    if pay > outstanding + 0.009:
        raise ValueError("amount_exceeds_outstanding")
    row = VatRemittance(
        tenant_id=tenant_id,
        year=year,
        quarter=quarter,
        amount_eur=round(pay, 2),
        notes=notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


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


async def update_invoice_status(
    db: AsyncSession,
    row: Invoice,
    *,
    status: str,
    lines: list[InvoiceLine] | None = None,
) -> Invoice:
    allowed = {"draft", "issued", "paid", "returned"}
    if status not in allowed:
        raise ValueError("invalid_status")
    transitions = {
        "draft": {"issued"},
        "issued": {"paid", "returned"},
        "paid": {"issued"},
        "returned": {"draft"},
    }
    if status != row.status and status not in transitions.get(row.status, set()):
        raise ValueError("invalid_transition")

    now = datetime.now(timezone.utc)
    if status == "issued" and row.status == "draft":
        row.issued_at = now
        terms = int(row.payment_terms_days or 30)
        row.due_date = now + timedelta(days=terms)
        row.returned_at = None
        if lines is not None:
            from app.services.pdf import generate_invoice_pdf

            row.pdf_path = generate_invoice_pdf(row, lines)
    elif status == "returned":
        row.returned_at = now

    row.status = status
    await db.commit()
    await db.refresh(row)
    return row


async def invoice_agenda(
    db: AsyncSession,
    *,
    tenant_id: str,
    week_start: date,
) -> list[dict]:
    week_end = week_start + timedelta(days=6)
    today = datetime.now(timezone.utc).date()

    rows = await db.scalars(
        select(Invoice).where(
            Invoice.tenant_id == tenant_id,
            Invoice.status == "issued",
        )
    )

    out: list[dict] = []
    seen: set[str] = set()
    for inv in rows:
        if inv.due_date is None:
            continue
        due = inv.due_date.date() if inv.due_date.tzinfo else inv.due_date.replace(tzinfo=timezone.utc).date()
        overdue = due < today
        in_week = week_start <= due <= week_end
        if not overdue and not in_week:
            continue
        if inv.id in seen:
            continue
        seen.add(inv.id)
        days_until = (due - today).days
        out.append(
            {
                "invoice_id": inv.id,
                "invoice_number": inv.invoice_number or inv.id[:8],
                "customer_name": inv.customer_name,
                "amount_eur": float(inv.amount_eur or 0),
                "due_date": due.isoformat(),
                "days_until_due": days_until,
                "overdue": overdue,
                "has_pdf": bool(inv.pdf_path),
            }
        )

    out.sort(key=lambda r: (not r["overdue"], r["due_date"]))
    return out
