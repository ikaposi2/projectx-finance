from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import CompanyProfile, CompensationEffect, Invoice, InvoiceLine
from app.services.clients import UpstreamError, fetch_customer, fetch_project

settings = get_settings()

PROGRESS_RANK = {"none": 0, "25": 25, "50": 50, "75": 75, "complete": 100}


class BillingError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def _format_address(parts: list[str | None]) -> str | None:
    cleaned = [p.strip() for p in parts if p and str(p).strip()]
    return ", ".join(cleaned) if cleaned else None


async def get_or_create_company(db: AsyncSession, tenant_id: str) -> CompanyProfile:
    row = await db.get(CompanyProfile, tenant_id)
    if row is not None:
        return row
    row = CompanyProfile(
        tenant_id=tenant_id,
        legal_name=settings.company_legal_name or "Platform BV",
        address_line1=settings.company_address or None,
        vat_id=settings.company_vat_id or None,
        coc_number=settings.company_coc_number or None,
        bank_account=settings.company_bank_account or None,
        invoice_email=settings.company_invoice_email or None,
        payment_terms_days=30,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_company(db: AsyncSession, row: CompanyProfile, data: dict) -> CompanyProfile:
    for key, value in data.items():
        if value is None and key in ("legal_name",):
            continue
        setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return row


async def next_invoice_number(db: AsyncSession, tenant_id: str) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"INV-{year}-"
    result = await db.scalar(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.tenant_id == tenant_id, Invoice.invoice_number.like(f"{prefix}%"))
    )
    seq = int(result or 0) + 1
    return f"{prefix}{seq:04d}"


async def billed_amount_for_project(db: AsyncSession, *, tenant_id: str, project_id: str) -> float:
    rows = await db.scalars(
        select(Invoice).where(
            Invoice.tenant_id == tenant_id,
            Invoice.project_id == project_id,
            Invoice.status.in_(("draft", "issued", "paid")),
            Invoice.kind.in_(("fixed_completion", "fixed_milestone_50")),
        )
    )
    return round(sum(float(r.subtotal_eur or r.amount_eur or 0) for r in rows), 2)


async def has_milestone_invoice(db: AsyncSession, *, tenant_id: str, project_id: str) -> bool:
    row = await db.scalar(
        select(Invoice.id).where(
            Invoice.tenant_id == tenant_id,
            Invoice.project_id == project_id,
            Invoice.kind == "fixed_milestone_50",
            Invoice.status.in_(("draft", "issued", "paid")),
        ).limit(1)
    )
    return row is not None


async def billed_time_entry_ids(db: AsyncSession, *, tenant_id: str, project_id: str) -> set[str]:
    inv_ids = await db.scalars(
        select(Invoice.id).where(
            Invoice.tenant_id == tenant_id,
            Invoice.project_id == project_id,
            Invoice.kind == "tm_hours",
            Invoice.status.in_(("draft", "issued", "paid")),
        )
    )
    ids = list(inv_ids)
    if not ids:
        return set()
    lines = await db.scalars(
        select(InvoiceLine.time_entry_id).where(
            InvoiceLine.tenant_id == tenant_id,
            InvoiceLine.invoice_id.in_(ids),
            InvoiceLine.time_entry_id.is_not(None),
        )
    )
    return {str(x) for x in lines if x}


async def unbilled_billable_effects(
    db: AsyncSession, *, tenant_id: str, project_id: str
) -> list[CompensationEffect]:
    billed = await billed_time_entry_ids(db, tenant_id=tenant_id, project_id=project_id)
    rows = await db.scalars(
        select(CompensationEffect).where(
            CompensationEffect.tenant_id == tenant_id,
            CompensationEffect.project_id == project_id,
            CompensationEffect.classification == "billable",
            CompensationEffect.applied.is_(True),
        )
    )
    return [r for r in rows if r.time_entry_id not in billed]


def _buyer_from_customer(customer: dict | None, fallback_name: str) -> tuple[str, str | None, str | None, str | None, int]:
    if not customer:
        return fallback_name, None, None, None, 30
    bill_to_name = str(customer.get("bill_to_name") or customer.get("name") or fallback_name)
    bill_to_id = customer.get("bill_to_customer_id") or customer.get("id")
    # If bill_to is parent, customer payload already resolves name; address from self unless we fetched bill-to
    same = customer.get("billing_same_as_address", True)
    if same:
        address = _format_address(
            [
                customer.get("address_line1"),
                customer.get("address_line2"),
                customer.get("postal_code"),
                customer.get("city"),
                customer.get("country"),
            ]
        )
    else:
        address = _format_address(
            [
                customer.get("billing_address_line1"),
                customer.get("billing_address_line2"),
                customer.get("billing_postal_code"),
                customer.get("billing_city"),
                customer.get("billing_country"),
            ]
        )
    vat = customer.get("vat_id")
    name = customer.get("billing_name") or bill_to_name
    terms = int(customer.get("payment_terms_days") or 30)
    return str(name), str(bill_to_id) if bill_to_id else None, vat, address, terms


async def resolve_actions_for_project(
    db: AsyncSession, *, tenant_id: str, project: dict
) -> list[dict]:
    project_id = str(project.get("id") or "")
    fixed = float(project.get("fixed_price_eur") or 0)
    progress = str(project.get("progress") or "none")
    rank = PROGRESS_RANK.get(progress, 0)
    actions: list[dict] = []

    already = await billed_amount_for_project(db, tenant_id=tenant_id, project_id=project_id)
    remaining_fixed = round(max(0.0, fixed - already), 2)

    if fixed > float(settings.milestone_threshold_eur) and rank >= 50:
        if not await has_milestone_invoice(db, tenant_id=tenant_id, project_id=project_id):
            milestone = round(fixed * 0.5, 2)
            if milestone > 0 and already < milestone:
                actions.append(
                    {
                        "kind": "fixed_milestone_50",
                        "label": "50% milestone invoice",
                        "amount_eur": milestone,
                        "enabled": True,
                    }
                )

    if progress == "complete" and remaining_fixed > 0.009:
        actions.append(
            {
                "kind": "fixed_completion",
                "label": "Final fixed-price invoice",
                "amount_eur": remaining_fixed,
                "enabled": True,
            }
        )

    unbilled = await unbilled_billable_effects(db, tenant_id=tenant_id, project_id=project_id)
    hours = round(sum(float(e.hours) for e in unbilled), 2)
    if hours > 0:
        staffing = project.get("staffing") or []
        avg_rate = 0.0
        if staffing:
            avg_rate = sum(float(s.get("rate_eur") or 0) for s in staffing) / len(staffing)
        actions.append(
            {
                "kind": "tm_hours",
                "label": "Consultancy hours invoice",
                "amount_eur": round(hours * avg_rate, 2) if avg_rate > 0 else 0.0,
                "hours": hours,
                "rate_eur": round(avg_rate, 2),
                "enabled": avg_rate > 0,
            }
        )

    return actions


async def list_billing_candidates(
    db: AsyncSession,
    *,
    tenant_id: str,
    access_token: str,
    projects: list[dict],
) -> list[dict]:
    out: list[dict] = []
    for brief in projects:
        pid = str(brief.get("id") or "")
        if not pid:
            continue
        try:
            detail = await fetch_project(project_id=pid, access_token=access_token)
        except UpstreamError:
            continue
        actions = await resolve_actions_for_project(db, tenant_id=tenant_id, project=detail)
        if not actions:
            continue
        out.append(
            {
                "project_id": pid,
                "project_name": detail.get("name") or brief.get("name"),
                "customer_id": detail.get("customer_id"),
                "customer_name": detail.get("customer_name") or brief.get("customer_name"),
                "fixed_price_eur": float(detail.get("fixed_price_eur") or 0),
                "progress": detail.get("progress") or "none",
                "report_url": detail.get("report_url"),
                "actions": actions,
            }
        )
    return out


async def generate_invoice(
    db: AsyncSession,
    *,
    tenant_id: str,
    access_token: str,
    project_id: str,
    kind: str,
    description: str | None = None,
    period_label: str | None = None,
) -> Invoice:
    project = await fetch_project(project_id=project_id, access_token=access_token)
    actions = await resolve_actions_for_project(db, tenant_id=tenant_id, project=project)
    action = next((a for a in actions if a["kind"] == kind and a.get("enabled")), None)
    if action is None:
        raise BillingError("billing_not_available")

    company = await get_or_create_company(db, tenant_id)
    customer = None
    if project.get("customer_id"):
        customer = await fetch_customer(customer_id=str(project["customer_id"]), access_token=access_token)
        # Prefer bill-to party record when child account
        bill_to_id = customer.get("bill_to_customer_id") if customer else None
        if bill_to_id and customer and bill_to_id != customer.get("id"):
            bill_to = await fetch_customer(customer_id=str(bill_to_id), access_token=access_token)
            if bill_to:
                customer = bill_to

    buyer_name, buyer_id, buyer_vat, buyer_address, terms = _buyer_from_customer(
        customer, str(project.get("customer_name") or "Customer")
    )
    seller_address = _format_address(
        [
            company.address_line1,
            company.address_line2,
            company.postal_code,
            company.city,
            company.country,
        ]
    ) or settings.company_address or None

    lines: list[dict] = []
    if kind == "fixed_milestone_50":
        amount = float(action["amount_eur"])
        lines.append(
            {
                "description": description
                or f"Milestone 50% — {project.get('name')} (assignment €{float(project.get('fixed_price_eur') or 0):,.2f})",
                "quantity": 1.0,
                "unit": "lump",
                "unit_price_eur": amount,
                "amount_eur": amount,
                "source": "milestone",
                "time_entry_id": None,
            }
        )
    elif kind == "fixed_completion":
        amount = float(action["amount_eur"])
        report = project.get("report_url")
        desc = description or f"Fixed-price completion — {project.get('name')}"
        if report:
            desc = f"{desc}. Client report: {report}"
        lines.append(
            {
                "description": desc,
                "quantity": 1.0,
                "unit": "lump",
                "unit_price_eur": amount,
                "amount_eur": amount,
                "source": "completion",
                "time_entry_id": None,
            }
        )
    elif kind == "tm_hours":
        unbilled = await unbilled_billable_effects(db, tenant_id=tenant_id, project_id=project_id)
        staffing = project.get("staffing") or []
        rate_by_partner = {str(s.get("partner_id")): float(s.get("rate_eur") or 0) for s in staffing}
        avg_rate = float(action.get("rate_eur") or 0)
        period = period_label or datetime.now(timezone.utc).strftime("%Y-%m")
        for effect in unbilled:
            rate = rate_by_partner.get(effect.partner_id) or avg_rate
            if rate <= 0:
                continue
            hrs = float(effect.hours)
            lines.append(
                {
                    "description": description
                    or f"Consultancy hours — {project.get('name')} ({period})",
                    "quantity": hrs,
                    "unit": "hour",
                    "unit_price_eur": rate,
                    "amount_eur": round(hrs * rate, 2),
                    "source": "approved_hours",
                    "time_entry_id": effect.time_entry_id,
                }
            )
        if not lines:
            raise BillingError("no_billable_hours")
    else:
        raise BillingError("invalid_kind")

    subtotal = round(sum(float(l["amount_eur"]) for l in lines), 2)
    vat_rate = float(settings.default_vat_rate)
    vat_eur = round(subtotal * (vat_rate / 100.0), 2)
    total = round(subtotal + vat_eur, 2)

    invoice = Invoice(
        tenant_id=tenant_id,
        invoice_number=await next_invoice_number(db, tenant_id),
        kind=kind,
        project_id=project_id,
        project_name=str(project.get("name") or ""),
        customer_id=buyer_id,
        customer_name=buyer_name,
        buyer_vat_id=buyer_vat,
        buyer_address=buyer_address,
        seller_name=company.legal_name,
        seller_vat_id=company.vat_id,
        seller_address=seller_address,
        seller_bank_account=company.bank_account,
        description=description,
        period_label=period_label,
        subtotal_eur=subtotal,
        vat_rate=vat_rate,
        vat_eur=vat_eur,
        amount_eur=total,
        payment_terms_days=terms or company.payment_terms_days,
        status="draft",
    )
    db.add(invoice)
    await db.flush()
    for line in lines:
        db.add(
            InvoiceLine(
                invoice_id=invoice.id,
                tenant_id=tenant_id,
                description=line["description"],
                quantity=line["quantity"],
                unit=line["unit"],
                unit_price_eur=line["unit_price_eur"],
                amount_eur=line["amount_eur"],
                source=line["source"],
                time_entry_id=line["time_entry_id"],
            )
        )
    await db.commit()
    await db.refresh(invoice)
    return invoice


async def list_invoice_lines(db: AsyncSession, invoice_id: str) -> list[InvoiceLine]:
    result = await db.scalars(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id).order_by(InvoiceLine.description)
    )
    return list(result)
