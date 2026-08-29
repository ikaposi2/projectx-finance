"""Monthly factuurvoorstel (draft invoice) for external resources."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import InboxMessage, Invoice, InvoiceLine
from app.services.billing import BillingError, _format_address, get_or_create_company
from app.services.clients import fetch_resources, fetch_time_entries
from app.services.pdf import generate_invoice_pdf

settings = get_settings()

PERSONNEL_KIND = "personnel_proposal"


def _month_bounds(month: str) -> tuple[str, str]:
    """Return (from_iso, to_iso) for YYYY-MM."""
    year = int(month[:4])
    mon = int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def _seller_address(resource: dict) -> str | None:
    return _format_address(
        [
            resource.get("address_line1"),
            resource.get("address_line2"),
            resource.get("postal_code"),
            resource.get("city"),
            resource.get("country"),
        ]
    )


def _buyer_address(company) -> str | None:
    return _format_address(
        [
            company.address_line1,
            company.address_line2,
            company.postal_code,
            company.city,
            company.country,
        ]
    ) or settings.company_address or None


async def next_proposal_number(db: AsyncSession, tenant_id: str) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"PROP-{year}-"
    rows = await db.scalars(
        select(Invoice.invoice_number).where(
            Invoice.tenant_id == tenant_id,
            Invoice.invoice_number.like(f"{prefix}%"),
        )
    )
    max_seq = 0
    for num in rows:
        if not num:
            continue
        suffix = str(num)[len(prefix) :]
        try:
            max_seq = max(max_seq, int(suffix))
        except ValueError:
            continue
    return f"{prefix}{max_seq + 1:04d}"


async def list_personnel_proposals(
    db: AsyncSession,
    *,
    tenant_id: str,
    month: str | None = None,
) -> list[Invoice]:
    stmt = select(Invoice).where(
        Invoice.tenant_id == tenant_id,
        Invoice.kind == PERSONNEL_KIND,
    )
    if month:
        stmt = stmt.where(Invoice.period_label == month)
    result = await db.scalars(stmt.order_by(Invoice.created_at.desc()))
    return list(result)


async def latest_personnel_proposal(
    db: AsyncSession,
    *,
    tenant_id: str,
    partner_id: str,
    month: str,
) -> Invoice | None:
    return await db.scalar(
        select(Invoice)
        .where(
            Invoice.tenant_id == tenant_id,
            Invoice.kind == PERSONNEL_KIND,
            Invoice.partner_id == partner_id,
            Invoice.period_label == month,
            Invoice.status.in_(("draft", "issued", "paid")),
        )
        .order_by(Invoice.created_at.desc())
        .limit(1)
    )


async def _personnel_proposal_invoice_ids(
    db: AsyncSession,
    *,
    tenant_id: str,
    partner_id: str,
    month: str,
) -> list[str]:
    rows = await db.scalars(
        select(Invoice.id).where(
            Invoice.tenant_id == tenant_id,
            Invoice.kind == PERSONNEL_KIND,
            Invoice.partner_id == partner_id,
            Invoice.period_label == month,
            Invoice.status.in_(("draft", "issued", "paid")),
        )
    )
    return [str(i) for i in rows]


async def personnel_billed_time_entry_ids(
    db: AsyncSession,
    *,
    tenant_id: str,
    partner_id: str,
    month: str,
) -> set[str]:
    inv_ids = await _personnel_proposal_invoice_ids(
        db, tenant_id=tenant_id, partner_id=partner_id, month=month
    )
    if not inv_ids:
        return set()
    lines = await db.scalars(
        select(InvoiceLine.time_entry_id).where(
            InvoiceLine.tenant_id == tenant_id,
            InvoiceLine.invoice_id.in_(inv_ids),
            InvoiceLine.time_entry_id.is_not(None),
        )
    )
    return {str(x) for x in lines if x}


async def personnel_legacy_proposed_hours(
    db: AsyncSession,
    *,
    tenant_id: str,
    partner_id: str,
    month: str,
) -> float:
    """
    Hours covered by legacy aggregate personnel lines (no time_entry_id).

    Older proposals used one summary line; treat that quantity as FIFO-consumed
    approved billable hours for the partner in the month.
    """
    inv_ids = await _personnel_proposal_invoice_ids(
        db, tenant_id=tenant_id, partner_id=partner_id, month=month
    )
    if not inv_ids:
        return 0.0
    total = 0.0
    for inv_id in inv_ids:
        lines = list(
            await db.scalars(
                select(InvoiceLine).where(
                    InvoiceLine.tenant_id == tenant_id,
                    InvoiceLine.invoice_id == inv_id,
                )
            )
        )
        if not lines:
            continue
        if any(line.time_entry_id for line in lines):
            continue
        total += sum(float(line.quantity or 0) for line in lines)
    return round(total, 4)


def _is_past_work_date(work_date: str) -> bool:
    """Only hours on or before today can be proposed for payment."""
    try:
        return date.fromisoformat(str(work_date)[:10]) <= date.today()
    except ValueError:
        return False


def _approved_billable_entries(entries: list[dict], *, partner_id: str) -> list[dict]:
    rows = [
        e
        for e in entries
        if str(e.get("partner_id") or "") == partner_id
        and e.get("status") == "approved"
        and e.get("classification") == "billable"
        and float(e.get("hours") or 0) > 0
        and _is_past_work_date(str(e.get("work_date") or ""))
    ]
    return sorted(rows, key=lambda e: (str(e.get("work_date") or ""), str(e.get("id") or "")))


def unbilled_personnel_entries(
    entries: list[dict],
    *,
    partner_id: str,
    billed_entry_ids: set[str],
    legacy_hours_remaining: float,
) -> list[dict[str, Any]]:
    unbilled: list[dict[str, Any]] = []
    legacy_left = max(0.0, legacy_hours_remaining)
    for entry in _approved_billable_entries(entries, partner_id=partner_id):
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in billed_entry_ids:
            continue
        hours = float(entry["hours"])
        if legacy_left > 0:
            if legacy_left >= hours - 1e-9:
                legacy_left = round(legacy_left - hours, 4)
                continue
            remaining = round(hours - legacy_left, 4)
            legacy_left = 0.0
            if remaining > 0:
                unbilled.append({**entry, "hours": remaining})
            continue
        unbilled.append(dict(entry))
    return unbilled


async def personnel_candidates(
    db: AsyncSession,
    *,
    tenant_id: str,
    access_token: str,
    month: str,
) -> list[dict]:
    """External resources with unbilled approved billable hours in the month."""
    from_day, to_day = _month_bounds(month)
    resources = await fetch_resources(access_token=access_token)
    externals = [r for r in resources if (r.get("kind") or "external") == "external" and r.get("active", True)]
    if not externals:
        return []

    entries = await fetch_time_entries(access_token=access_token, from_date=from_day, to_date=to_day)

    vat_rate = float(settings.default_vat_rate)
    out: list[dict] = []
    for resource in externals:
        partner_id = str(resource.get("partner_id") or "")
        billed_ids = await personnel_billed_time_entry_ids(
            db, tenant_id=tenant_id, partner_id=partner_id, month=month
        )
        legacy_hours = await personnel_legacy_proposed_hours(
            db, tenant_id=tenant_id, partner_id=partner_id, month=month
        )
        unbilled = unbilled_personnel_entries(
            entries,
            partner_id=partner_id,
            billed_entry_ids=billed_ids,
            legacy_hours_remaining=legacy_hours,
        )
        hours = round(sum(float(e.get("hours") or 0) for e in unbilled), 2)
        if hours <= 0:
            continue
        rate = float(resource.get("billable_rate_eur") or settings.internal_rate_eur)
        subtotal = round(hours * rate, 2)
        vat_eur = round(subtotal * (vat_rate / 100.0), 2)
        existing = await latest_personnel_proposal(
            db, tenant_id=tenant_id, partner_id=partner_id, month=month
        )
        out.append(
            {
                "partner_id": partner_id,
                "resource_id": resource.get("id"),
                "display_name": resource.get("display_name") or partner_id,
                "month": month,
                "hours": hours,
                "rate_eur": rate,
                "subtotal_eur": subtotal,
                "vat_rate": vat_rate,
                "vat_eur": vat_eur,
                "total_eur": round(subtotal + vat_eur, 2),
                "already_generated": existing is not None,
                "invoice_id": existing.id if existing else None,
                "invoice_number": existing.invoice_number if existing else None,
            }
        )
    out.sort(key=lambda r: str(r["display_name"]).lower())
    return out


async def generate_personnel_proposal(
    db: AsyncSession,
    *,
    tenant_id: str,
    access_token: str,
    partner_id: str,
    month: str,
) -> Invoice:
    if len(month) != 7 or month[4] != "-":
        raise BillingError("invalid_month")

    resources = await fetch_resources(access_token=access_token)
    resource = next(
        (
            r
            for r in resources
            if str(r.get("partner_id") or "") == partner_id
            and (r.get("kind") or "external") == "external"
        ),
        None,
    )
    if resource is None:
        raise BillingError("resource_not_found")

    from_day, to_day = _month_bounds(month)
    entries = await fetch_time_entries(access_token=access_token, from_date=from_day, to_date=to_day)
    billed_ids = await personnel_billed_time_entry_ids(
        db, tenant_id=tenant_id, partner_id=partner_id, month=month
    )
    legacy_hours = await personnel_legacy_proposed_hours(
        db, tenant_id=tenant_id, partner_id=partner_id, month=month
    )
    unbilled = unbilled_personnel_entries(
        entries,
        partner_id=partner_id,
        billed_entry_ids=billed_ids,
        legacy_hours_remaining=legacy_hours,
    )
    if not unbilled:
        raise BillingError("no_hours_for_month")

    rate = float(resource.get("billable_rate_eur") or settings.internal_rate_eur)
    vat_rate = float(settings.default_vat_rate)

    buyer = await get_or_create_company(db, tenant_id)
    seller_company = (resource.get("company_name") or "").strip()
    person = (resource.get("display_name") or "").strip()
    seller_name = seller_company or person or "External consultant"
    if seller_company and person and seller_company.lower() != person.lower():
        seller_name = f"{seller_company} ({person})"
    seller_address = _seller_address(resource)
    seller_vat = (resource.get("vat_id") or None) and str(resource.get("vat_id"))
    seller_bank = (resource.get("bank_account") or None) and str(resource.get("bank_account"))
    buyer_name = buyer.legal_name or settings.company_legal_name
    buyer_address = _buyer_address(buyer)
    buyer_vat = buyer.vat_id or settings.company_vat_id or None

    hours = round(sum(float(e.get("hours") or 0) for e in unbilled), 2)
    subtotal = round(hours * rate, 2)
    vat_eur = round(subtotal * (vat_rate / 100.0), 2)
    total = round(subtotal + vat_eur, 2)

    month_label = datetime.strptime(f"{month}-01", "%Y-%m-%d").strftime("%B %Y")
    description = (
        f"Factuurvoorstel — consultancy hours {month_label}. "
        f"Rate €{rate:,.2f}/h ex VAT."
    )

    invoice = Invoice(
        tenant_id=tenant_id,
        invoice_number=await next_proposal_number(db, tenant_id),
        kind=PERSONNEL_KIND,
        project_id=None,
        project_name="",
        customer_id=None,
        customer_name=buyer_name,
        partner_id=partner_id,
        buyer_vat_id=buyer_vat,
        buyer_address=buyer_address,
        seller_name=seller_name,
        seller_vat_id=seller_vat,
        seller_address=seller_address,
        seller_bank_account=seller_bank,
        description=description,
        period_label=month,
        subtotal_eur=subtotal,
        vat_rate=vat_rate,
        vat_eur=vat_eur,
        amount_eur=total,
        payment_terms_days=int(buyer.payment_terms_days or 30),
        status="draft",
        notes=None,
    )
    db.add(invoice)
    await db.flush()

    line_models: list[InvoiceLine] = []
    for entry in unbilled:
        entry_hours = float(entry.get("hours") or 0)
        if entry_hours <= 0:
            continue
        work_date = str(entry.get("work_date") or "")
        line = InvoiceLine(
            invoice_id=invoice.id,
            tenant_id=tenant_id,
            description=f"Consultancy hours — {work_date} ({entry_hours:g} h × €{rate:,.2f})",
            quantity=entry_hours,
            unit="hour",
            unit_price_eur=rate,
            amount_eur=round(entry_hours * rate, 2),
            source="personnel_hours",
            time_entry_id=str(entry.get("id") or "") or None,
        )
        db.add(line)
        line_models.append(line)
    await db.flush()

    if not line_models:
        raise BillingError("no_hours_for_month")

    invoice.issued_at = datetime.now(timezone.utc)
    invoice.pdf_path = generate_invoice_pdf(invoice, line_models, document_title="FACTUURVOORSTEL")
    db.add(
        InboxMessage(
            tenant_id=tenant_id,
            user_id=partner_id,
            invoice_id=invoice.id,
            kind=PERSONNEL_KIND,
            title=f"{invoice.invoice_number} — {month_label}",
        )
    )
    await db.commit()
    await db.refresh(invoice)
    return invoice


def previous_calendar_month(today: date | None = None) -> str:
    ref = today or date.today()
    year, month = ref.year, ref.month - 1
    if month < 1:
        month = 12
        year -= 1
    return f"{year:04d}-{month:02d}"


async def generate_monthly_personnel_proposals(
    db: AsyncSession,
    *,
    tenant_id: str,
    access_token: str,
    month: str,
) -> dict[str, int]:
    """Create proposals for all externals with unbilled past hours in the month."""
    candidates = await personnel_candidates(
        db, tenant_id=tenant_id, access_token=access_token, month=month
    )
    created = 0
    skipped = 0
    errors = 0
    for cand in candidates:
        if float(cand.get("hours") or 0) <= 0:
            skipped += 1
            continue
        try:
            await generate_personnel_proposal(
                db,
                tenant_id=tenant_id,
                access_token=access_token,
                partner_id=str(cand["partner_id"]),
                month=month,
            )
            created += 1
        except BillingError:
            skipped += 1
        except Exception:
            errors += 1
    return {"month": month, "created": created, "skipped": skipped, "errors": errors}
