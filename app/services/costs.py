"""Monthly operational costs (one-off and recurring)."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MonthlyCost

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class CostError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def _validate_month(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if not _MONTH_RE.match(v):
        raise CostError(f"invalid_{field}")
    return v


def applies_to_month(row: MonthlyCost, month: str) -> bool:
    if row.cadence == "one_off":
        return row.start_month == month
    if row.start_month > month:
        return False
    if row.end_month and row.end_month < month:
        return False
    return True


async def list_costs_for_month(
    db: AsyncSession,
    *,
    tenant_id: str,
    month: str,
) -> list[MonthlyCost]:
    month_n = _validate_month(month, field="month")
    if not month_n:
        raise CostError("invalid_month")
    result = await db.scalars(
        select(MonthlyCost)
        .where(MonthlyCost.tenant_id == tenant_id)
        .order_by(MonthlyCost.label, MonthlyCost.start_month)
    )
    return [r for r in result if applies_to_month(r, month_n)]


async def create_cost(
    db: AsyncSession,
    *,
    tenant_id: str,
    label: str,
    amount_eur: float,
    cadence: str,
    start_month: str,
    end_month: str | None = None,
    notes: str | None = None,
) -> MonthlyCost:
    label_n = (label or "").strip()
    if not label_n:
        raise CostError("label_required")
    if amount_eur < 0:
        raise CostError("invalid_amount")
    cadence_n = (cadence or "one_off").strip()
    if cadence_n not in {"one_off", "recurring"}:
        raise CostError("invalid_cadence")
    start = _validate_month(start_month, field="start_month")
    if not start:
        raise CostError("invalid_start_month")
    end = _validate_month(end_month, field="end_month")
    if cadence_n == "one_off":
        end = None
    elif end and end < start:
        raise CostError("invalid_end_month")

    row = MonthlyCost(
        tenant_id=tenant_id,
        label=label_n,
        amount_eur=float(amount_eur),
        cadence=cadence_n,
        start_month=start,
        end_month=end,
        notes=(notes or "").strip() or None,
        invoice_matched=False,
        invoice_paid=False,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_cost(
    db: AsyncSession,
    *,
    tenant_id: str,
    cost_id: str,
) -> MonthlyCost | None:
    return await db.scalar(
        select(MonthlyCost).where(MonthlyCost.id == cost_id, MonthlyCost.tenant_id == tenant_id)
    )


async def update_cost(
    db: AsyncSession,
    row: MonthlyCost,
    *,
    label: str | None = None,
    amount_eur: float | None = None,
    cadence: str | None = None,
    start_month: str | None = None,
    end_month: str | None = None,
    clear_end_month: bool = False,
    notes: str | None = None,
    invoice_matched: bool | None = None,
    invoice_paid: bool | None = None,
) -> MonthlyCost:
    if label is not None:
        label_n = label.strip()
        if not label_n:
            raise CostError("label_required")
        row.label = label_n
    if amount_eur is not None:
        if amount_eur < 0:
            raise CostError("invalid_amount")
        row.amount_eur = float(amount_eur)
    if cadence is not None:
        if cadence not in {"one_off", "recurring"}:
            raise CostError("invalid_cadence")
        row.cadence = cadence
        if cadence == "one_off":
            row.end_month = None
    if start_month is not None:
        start = _validate_month(start_month, field="start_month")
        if not start:
            raise CostError("invalid_start_month")
        row.start_month = start
    if clear_end_month:
        row.end_month = None
    elif end_month is not None:
        end = _validate_month(end_month, field="end_month")
        row.end_month = end
    if notes is not None:
        row.notes = notes.strip() or None
    if invoice_matched is not None:
        row.invoice_matched = invoice_matched
    if invoice_paid is not None:
        row.invoice_paid = invoice_paid
    if row.cadence == "recurring" and row.end_month and row.end_month < row.start_month:
        raise CostError("invalid_end_month")
    await db.commit()
    await db.refresh(row)
    return row


async def delete_cost(db: AsyncSession, row: MonthlyCost) -> None:
    await db.delete(row)
    await db.commit()
