"""Monthly factuurvoorstel (draft invoice) for external resources."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Invoice, InvoiceLine
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


async def existing_proposal(
    db: AsyncSession,
    *,
    tenant_id: str,
    partner_id: str,
    month: str,
) -> Invoice | None:
    return await db.scalar(
        select(Invoice).where(
            Invoice.tenant_id == tenant_id,
            Invoice.kind == PERSONNEL_KIND,
            Invoice.partner_id == partner_id,
            Invoice.period_label == month,
            Invoice.status.in_(("draft", "issued", "paid")),
        )
    )


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


async def personnel_candidates(
    db: AsyncSession,
    *,
    tenant_id: str,
    access_token: str,
    month: str,
) -> list[dict]:
    """External resources with approved hours in the month (by work_date)."""
    from_day, to_day = _month_bounds(month)
    resources = await fetch_resources(access_token=access_token)
    externals = [r for r in resources if (r.get("kind") or "external") == "external" and r.get("active", True)]
    if not externals:
        return []

    entries = await fetch_time_entries(access_token=access_token, from_date=from_day, to_date=to_day)
    approved = [
        e
        for e in entries
        if e.get("status") == "approved" and float(e.get("hours") or 0) > 0
    ]

    by_partner: dict[str, float] = {}
    for e in approved:
        pid = str(e.get("partner_id") or "")
        if not pid:
            continue
        by_partner[pid] = by_partner.get(pid, 0.0) + float(e["hours"])

    vat_rate = float(settings.default_vat_rate)
    out: list[dict] = []
    for resource in externals:
        partner_id = str(resource.get("partner_id") or "")
        hours = round(by_partner.get(partner_id, 0.0), 2)
        if hours <= 0:
            continue
        rate = float(resource.get("internal_rate_eur") or settings.internal_rate_eur)
        subtotal = round(hours * rate, 2)
        vat_eur = round(subtotal * (vat_rate / 100.0), 2)
        existing = await existing_proposal(
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

    existing = await existing_proposal(
        db, tenant_id=tenant_id, partner_id=partner_id, month=month
    )
    if existing is not None:
        raise BillingError("proposal_already_exists")

    candidates = await personnel_candidates(
        db, tenant_id=tenant_id, access_token=access_token, month=month
    )
    cand = next((c for c in candidates if c["partner_id"] == partner_id), None)
    if cand is None:
        raise BillingError("no_hours_for_month")

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

    company = await get_or_create_company(db, tenant_id)
    seller_name = str(resource.get("display_name") or "External consultant")
    seller_address = _seller_address(resource)
    seller_vat = (resource.get("vat_id") or None) and str(resource.get("vat_id"))
    seller_bank = (resource.get("bank_account") or None) and str(resource.get("bank_account"))
    buyer_name = company.legal_name or settings.company_legal_name
    buyer_address = _buyer_address(company)
    buyer_vat = company.vat_id or settings.company_vat_id or None

    hours = float(cand["hours"])
    rate = float(cand["rate_eur"])
    subtotal = float(cand["subtotal_eur"])
    vat_rate = float(cand["vat_rate"])
    vat_eur = float(cand["vat_eur"])
    total = float(cand["total_eur"])

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
        payment_terms_days=int(company.payment_terms_days or 30),
        status="draft",
        notes=None,
    )
    db.add(invoice)
    await db.flush()

    line = InvoiceLine(
        invoice_id=invoice.id,
        tenant_id=tenant_id,
        description=f"Consultancy hours — {month_label} ({hours:g} h × €{rate:,.2f})",
        quantity=hours,
        unit="hour",
        unit_price_eur=rate,
        amount_eur=subtotal,
        source="personnel_hours",
        time_entry_id=None,
    )
    db.add(line)
    await db.flush()

    # Draft proposals get a PDF immediately so they can be sent as factuurvoorstel.
    invoice.issued_at = datetime.now(timezone.utc)
    lines = [line]
    invoice.pdf_path = generate_invoice_pdf(invoice, lines, document_title="FACTUURVOORSTEL")
    await db.commit()
    await db.refresh(invoice)
    return invoice
