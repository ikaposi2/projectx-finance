from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import get_settings
from app.db.models import Invoice, InvoiceLine

settings = get_settings()


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _archive_path(invoice: Invoice) -> Path:
    root = Path(settings.archive_root)
    safe_num = (invoice.invoice_number or invoice.id).replace("/", "-")
    rel = Path("invoices") / invoice.tenant_id / f"{safe_num}.pdf"
    return root / rel


def generate_invoice_pdf(invoice: Invoice, lines: list[InvoiceLine]) -> str:
    """Write PDF to archive PVC; return relative path under ARCHIVE_ROOT."""
    path = _archive_path(invoice)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm)
    styles = getSampleStyleSheet()
    story: list = []

    story.append(Paragraph(f"<b>Invoice {invoice.invoice_number}</b>", styles["Title"]))
    story.append(Spacer(1, 6 * mm))
    story.append(
        Paragraph(
            f"Issue date: {_fmt_date(invoice.issued_at)} &nbsp;|&nbsp; "
            f"Due date: {_fmt_date(invoice.due_date)} &nbsp;|&nbsp; "
            f"Payment terms: {invoice.payment_terms_days} days",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 8 * mm))

    seller = f"<b>{invoice.seller_name or 'Seller'}</b><br/>{invoice.seller_address or ''}"
    if invoice.seller_vat_id:
        seller += f"<br/>VAT: {invoice.seller_vat_id}"
    if invoice.seller_bank_account:
        seller += f"<br/>IBAN: {invoice.seller_bank_account}"
    buyer = f"<b>{invoice.customer_name}</b><br/>{invoice.buyer_address or ''}"
    if invoice.buyer_vat_id:
        buyer += f"<br/>VAT: {invoice.buyer_vat_id}"

    party_data = [
        [Paragraph("From", styles["Heading4"]), Paragraph("Bill to", styles["Heading4"])],
        [Paragraph(seller, styles["Normal"]), Paragraph(buyer, styles["Normal"])],
    ]
    party_table = Table(party_data, colWidths=[90 * mm, 90 * mm])
    party_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(party_table)
    story.append(Spacer(1, 10 * mm))

    if invoice.description:
        story.append(Paragraph(invoice.description, styles["Normal"]))
        story.append(Spacer(1, 6 * mm))

    table_data = [["Description", "Qty", "Unit", "Price", "Amount"]]
    for line in lines:
        unit = "h" if line.unit == "hour" else ""
        table_data.append(
            [
                line.description[:80],
                f"{line.quantity:g}",
                unit,
                f"€{line.unit_price_eur:,.2f}",
                f"€{line.amount_eur:,.2f}",
            ]
        )
    table_data.extend(
        [
            ["", "", "", "Subtotal", f"€{float(invoice.subtotal_eur):,.2f}"],
            ["", "", "", f"VAT ({invoice.vat_rate:g}%)", f"€{float(invoice.vat_eur):,.2f}"],
            ["", "", "", "Total", f"€{float(invoice.amount_eur):,.2f}"],
        ]
    )
    tbl = Table(table_data, colWidths=[80 * mm, 18 * mm, 15 * mm, 28 * mm, 28 * mm])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black),
                ("LINEABOVE", (3, -3), (-1, -1), 0.5, colors.grey),
                ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story.append(tbl)
    doc.build(story)

    rel = path.relative_to(Path(settings.archive_root))
    return str(rel).replace("\\", "/")


def resolve_pdf_absolute(relative_path: str) -> Path:
    return Path(settings.archive_root) / relative_path
