from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InboxMessage, Invoice
from app.services.object_store import presigned_pdf_url


async def list_inbox(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
) -> list[dict]:
    rows = await db.scalars(
        select(InboxMessage)
        .where(InboxMessage.tenant_id == tenant_id, InboxMessage.user_id == user_id)
        .order_by(InboxMessage.created_at.desc())
    )
    out: list[dict] = []
    for row in rows:
        invoice = await db.get(Invoice, row.invoice_id)
        out.append(
            {
                "id": row.id,
                "kind": row.kind,
                "title": row.title,
                "invoice_id": row.invoice_id,
                "invoice_number": invoice.invoice_number if invoice else "",
                "period_label": invoice.period_label if invoice else None,
                "amount_eur": float(invoice.amount_eur) if invoice else 0.0,
                "read": row.read_at is not None,
                "read_at": row.read_at.isoformat() if row.read_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return out


async def unread_inbox_count(db: AsyncSession, *, tenant_id: str, user_id: str) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(InboxMessage)
        .where(
            InboxMessage.tenant_id == tenant_id,
            InboxMessage.user_id == user_id,
            InboxMessage.read_at.is_(None),
        )
    )
    return int(count or 0)


async def open_inbox_message(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    message_id: str,
) -> dict | None:
    row = await db.get(InboxMessage, message_id)
    if row is None or row.tenant_id != tenant_id or row.user_id != user_id:
        return None
    invoice = await db.get(Invoice, row.invoice_id)
    if invoice is None or invoice.tenant_id != tenant_id:
        return None
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)

    pdf_url = f"/api/finance/inbox/invoices/{invoice.id}/pdf"
    if invoice.pdf_path:
        signed = presigned_pdf_url(invoice.pdf_path)
        if signed:
            pdf_url = signed

    return {
        "id": row.id,
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "pdf_url": pdf_url,
        "read": True,
        "read_at": row.read_at.isoformat() if row.read_at else None,
    }


async def get_partner_invoice_pdf(
    db: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    invoice_id: str,
) -> Invoice | None:
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None or invoice.tenant_id != tenant_id:
        return None
    if str(invoice.partner_id or "") != user_id:
        return None
    if not invoice.pdf_path:
        return None
    return invoice
