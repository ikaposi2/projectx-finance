"""Management reporting aggregates (Phase 5)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Invoice
from app.services.billing import billed_amount_for_project, unbilled_billable_effects
from app.services.clients import (
    UpstreamError,
    fetch_bookable_projects,
    fetch_funnel_summary,
    fetch_project,
    fetch_resources,
    fetch_time_entries,
)
from app.services.ledger import _invoice_net

settings = get_settings()

HOURS_PER_DAY = 8.0


def working_days(from_day: date, to_day: date) -> int:
    if to_day < from_day:
        return 0
    n = 0
    cur = from_day
    while cur <= to_day:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def _as_utc_day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _as_utc_day_end(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)


async def report_summary(
    db: AsyncSession,
    *,
    tenant_id: str,
    access_token: str,
    from_day: date,
    to_day: date,
) -> dict:
    if to_day < from_day:
        raise ValueError("invalid_range")

    funnel_rows = await fetch_funnel_summary(access_token=access_token)
    funnel = [
        {
            "funnel_status": str(r.get("funnel_status") or ""),
            "count": int(r.get("count") or 0),
            "value_eur": round(float(r.get("value_eur") or 0), 2),
            "remaining_hours": round(float(r.get("remaining_hours") or 0), 2),
            "contracted_hours": round(float(r.get("contracted_hours") or 0), 2),
        }
        for r in funnel_rows
    ]

    in_progress = await _in_progress_value(db, tenant_id=tenant_id, access_token=access_token)
    utilization = await _utilization(
        access_token=access_token, from_day=from_day, to_day=to_day
    )
    delivered, received = await _invoice_period_totals(
        db, tenant_id=tenant_id, from_day=from_day, to_day=to_day
    )

    return {
        "from_date": from_day.isoformat(),
        "to_date": to_day.isoformat(),
        "funnel": funnel,
        "in_progress": in_progress,
        "utilization": utilization,
        "delivered_eur": delivered,
        "received_eur": received,
    }


async def _in_progress_value(
    db: AsyncSession, *, tenant_id: str, access_token: str
) -> dict:
    """Remaining fixed on bookable (in delivery) + T&M unbilled WIP €."""
    try:
        projects = await fetch_bookable_projects(access_token=access_token, include_complete=False)
    except UpstreamError:
        projects = []

    fixed_remaining = 0.0
    tm_wip = 0.0
    fixed_count = 0
    tm_count = 0

    for brief in projects:
        pid = str(brief.get("id") or "")
        if not pid:
            continue
        fixed = float(brief.get("fixed_price_eur") or 0)
        if fixed > 0.009:
            billed = await billed_amount_for_project(db, tenant_id=tenant_id, project_id=pid)
            rem = round(max(0.0, fixed - billed), 2)
            fixed_remaining = round(fixed_remaining + rem, 2)
            fixed_count += 1
            continue

        tm_count += 1
        try:
            detail = await fetch_project(project_id=pid, access_token=access_token)
        except UpstreamError:
            detail = brief
        staffing = detail.get("staffing") or []
        rate_by_partner = {
            str(s.get("partner_id")): float(s.get("rate_eur") or 0) for s in staffing
        }
        avg_rate = 0.0
        if staffing:
            avg_rate = sum(float(s.get("rate_eur") or 0) for s in staffing) / len(staffing)
        effects = await unbilled_billable_effects(db, tenant_id=tenant_id, project_id=pid)
        for effect in effects:
            rate = rate_by_partner.get(effect.partner_id) or float(effect.rate_eur or 0) or avg_rate
            if rate <= 0:
                continue
            tm_wip = round(tm_wip + float(effect.hours) * rate, 2)

    total = round(fixed_remaining + tm_wip, 2)
    return {
        "total_eur": total,
        "fixed_remaining_eur": fixed_remaining,
        "tm_wip_eur": tm_wip,
        "project_count": fixed_count + tm_count,
        "fixed_project_count": fixed_count,
        "tm_project_count": tm_count,
    }


async def _utilization(*, access_token: str, from_day: date, to_day: date) -> dict:
    days = working_days(from_day, to_day)
    try:
        resources = await fetch_resources(access_token=access_token)
    except UpstreamError:
        resources = []
    active = [r for r in resources if r.get("active", True)]
    resource_count = len(active)
    capacity_hours = round(resource_count * HOURS_PER_DAY * days, 2)

    try:
        entries = await fetch_time_entries(
            access_token=access_token,
            from_date=from_day.isoformat(),
            to_date=to_day.isoformat(),
        )
    except UpstreamError:
        entries = []

    billable_hours = 0.0
    non_billable_hours = 0.0
    for e in entries:
        if e.get("status") != "approved":
            continue
        hrs = float(e.get("hours") or 0)
        if hrs <= 0:
            continue
        if e.get("classification") == "billable":
            billable_hours += hrs
        else:
            non_billable_hours += hrs
    billable_hours = round(billable_hours, 2)
    non_billable_hours = round(non_billable_hours, 2)

    pct = round((billable_hours / capacity_hours) * 100.0, 1) if capacity_hours > 0 else 0.0
    return {
        "billable_hours": billable_hours,
        "non_billable_hours": non_billable_hours,
        "capacity_hours": capacity_hours,
        "utilization_pct": pct,
        "resource_count": resource_count,
        "working_days": days,
        "hours_per_day": HOURS_PER_DAY,
    }


async def _invoice_period_totals(
    db: AsyncSession, *, tenant_id: str, from_day: date, to_day: date
) -> tuple[float, float]:
    """Delivered = issued+paid net with issued_at in range; received = paid net marked in range."""
    start = _as_utc_day_start(from_day)
    end = _as_utc_day_end(to_day)
    rows = await db.scalars(
        select(Invoice).where(
            Invoice.tenant_id == tenant_id,
            Invoice.kind != "personnel_proposal",
            Invoice.status.in_(("issued", "paid")),
        )
    )
    delivered = 0.0
    received = 0.0
    for inv in rows:
        issued = inv.issued_at
        if issued is not None:
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
            if start <= issued <= end:
                delivered = round(delivered + _invoice_net(inv), 2)
        if inv.status == "paid":
            # Mark-paid updates updated_at; fall back to issued_at.
            paid_at = inv.updated_at or inv.issued_at
            if paid_at is None:
                continue
            if paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            if start <= paid_at <= end:
                received = round(received + _invoice_net(inv), 2)
    return delivered, received
