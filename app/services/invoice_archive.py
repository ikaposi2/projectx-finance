"""Collect issued invoices by calendar quarter or year (for export / downstream automation)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Invoice
from app.services.object_store import presigned_pdf_url


def _as_utc_day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _as_utc_day_end(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)


def period_bounds(*, year: int, quarter: int | None) -> tuple[date, date, str]:
    if quarter is None:
        return date(year, 1, 1), date(year, 12, 31), str(year)
    if quarter == 1:
        return date(year, 1, 1), date(year, 3, 31), f"{year}-Q1"
    if quarter == 2:
        return date(year, 4, 1), date(year, 6, 30), f"{year}-Q2"
    if quarter == 3:
        return date(year, 7, 1), date(year, 9, 30), f"{year}-Q3"
    return date(year, 10, 1), date(year, 12, 31), f"{year}-Q4"


def _normalize_issued_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _pdf_urls(invoice: Invoice) -> tuple[bool, str | None, str | None]:
    if not invoice.pdf_path:
        return False, None, None
    signed = presigned_pdf_url(invoice.pdf_path)
    api_path = f"/api/finance/invoices/{invoice.id}/pdf"
    return True, signed or api_path, invoice.pdf_path


def _invoice_to_archive_item(invoice: Invoice) -> dict:
    has_pdf, pdf_url, pdf_path = _pdf_urls(invoice)
    issued = _normalize_issued_at(invoice.issued_at)
    paid_at: datetime | None = None
    if invoice.status == "paid":
        paid_at = _normalize_issued_at(invoice.updated_at or invoice.issued_at)
    return {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number or invoice.id[:8],
        "kind": invoice.kind or "manual",
        "status": invoice.status,
        "customer_id": invoice.customer_id,
        "customer_name": invoice.customer_name or "",
        "partner_id": invoice.partner_id,
        "project_id": invoice.project_id,
        "project_name": invoice.project_name or "",
        "period_label": invoice.period_label,
        "subtotal_eur": float(invoice.subtotal_eur or 0),
        "vat_eur": float(invoice.vat_eur or 0),
        "amount_eur": float(invoice.amount_eur or 0),
        "issued_at": issued.isoformat() if issued else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "paid_at": paid_at.isoformat() if paid_at else None,
        "has_pdf": has_pdf,
        "pdf_path": pdf_path,
        "pdf_url": pdf_url,
    }


async def collect_invoices_for_period(
    db: AsyncSession,
    *,
    tenant_id: str,
    year: int,
    quarter: int | None = None,
    statuses: set[str] | None = None,
    include_personnel: bool = False,
) -> dict:
    from_day, to_day, period_label = period_bounds(year=year, quarter=quarter)
    allowed_statuses = statuses or {"issued", "paid"}
    start = _as_utc_day_start(from_day)
    end = _as_utc_day_end(to_day)

    stmt = select(Invoice).where(Invoice.tenant_id == tenant_id)
    if not include_personnel:
        stmt = stmt.where(Invoice.kind != "personnel_proposal")
    rows = list(await db.scalars(stmt))

    items: list[dict] = []
    for inv in rows:
        if inv.status not in allowed_statuses:
            continue
        issued = _normalize_issued_at(inv.issued_at)
        if issued is None or issued < start or issued > end:
            continue
        items.append(_invoice_to_archive_item(inv))

    items.sort(key=lambda row: row["issued_at"] or "")

    total_subtotal = round(sum(row["subtotal_eur"] for row in items), 2)
    total_vat = round(sum(row["vat_eur"] for row in items), 2)
    total_amount = round(sum(row["amount_eur"] for row in items), 2)

    return {
        "year": year,
        "quarter": quarter,
        "period_label": period_label,
        "from_date": from_day.isoformat(),
        "to_date": to_day.isoformat(),
        "status_filter": sorted(allowed_statuses),
        "include_personnel": include_personnel,
        "invoice_count": len(items),
        "total_subtotal_eur": total_subtotal,
        "total_vat_eur": total_vat,
        "total_amount_eur": total_amount,
        "invoices": items,
    }
