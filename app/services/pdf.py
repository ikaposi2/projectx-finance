from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import get_settings
from app.db.models import Invoice, InvoiceLine

settings = get_settings()

ACCENT = colors.HexColor("#1e40af")
ACCENT_LIGHT = colors.HexColor("#dbeafe")
TEXT_MUTED = colors.HexColor("#64748b")
BORDER = colors.HexColor("#e2e8f0")


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d %b %Y")


def _address_html(raw: str | None) -> str:
    if not raw or not raw.strip():
        return "<font color='#94a3b8'><i>Address not on file</i></font>"
    lines = [ln.strip() for ln in raw.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = [p.strip() for p in lines[0].split(",") if p.strip()]
    return "<br/>".join(lines)


def _archive_path(invoice: Invoice) -> Path:
    root = Path(settings.archive_root)
    safe_num = (invoice.invoice_number or invoice.id).replace("/", "-")
    rel = Path("invoices") / invoice.tenant_id / f"{safe_num}.pdf"
    return root / rel


def _money(value: float) -> str:
    return f"€{float(value):,.2f}"


def generate_invoice_pdf(
    invoice: Invoice,
    lines: list[InvoiceLine],
    *,
    document_title: str | None = None,
) -> str:
    """Write PDF to archive PVC; return relative path under ARCHIVE_ROOT."""
    path = _archive_path(invoice)
    path.parent.mkdir(parents=True, exist_ok=True)

    is_proposal = (invoice.kind or "") == "personnel_proposal" or (
        document_title or ""
    ).upper().startswith("FACTUUR")
    title = document_title or ("FACTUURVOORSTEL" if is_proposal else "INVOICE")
    number_label = "Proposal no." if is_proposal else "Invoice no."

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "InvoiceTitle",
        parent=styles["Title"],
        fontSize=24 if is_proposal else 28,
        textColor=ACCENT,
        spaceAfter=2 * mm,
        fontName="Helvetica-Bold",
    )
    label_style = ParagraphStyle(
        "Label",
        parent=styles["Normal"],
        fontSize=8,
        textColor=TEXT_MUTED,
        fontName="Helvetica-Bold",
        spaceAfter=1 * mm,
        leading=10,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#0f172a"),
    )
    muted_style = ParagraphStyle(
        "Muted",
        parent=body_style,
        fontSize=9,
        textColor=TEXT_MUTED,
    )
    right_style = ParagraphStyle(
        "Right",
        parent=body_style,
        alignment=TA_RIGHT,
    )

    story: list = []

    # Header band
    header_left = Paragraph(f"<b>{title}</b>", title_style)
    header_right = Paragraph(
        f"<para align='right'><font size='11' color='#64748b'>{number_label}</font><br/>"
        f"<font size='16' color='#1e40af'><b>{invoice.invoice_number}</b></font></para>",
        right_style,
    )
    header = Table([[header_left, header_right]], colWidths=[100 * mm, 74 * mm])
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 2, ACCENT),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 8 * mm))

    if is_proposal:
        story.append(
            Paragraph(
                "<b>Draft invoice proposal</b> — for review. Not a final tax invoice until confirmed.",
                muted_style,
            )
        )
        story.append(Spacer(1, 4 * mm))

    # Dates row
    period = invoice.period_label or "—"
    meta_cells = [
        Paragraph(
            f"<b>{'Proposal date' if is_proposal else 'Issue date'}</b><br/>{_fmt_date(invoice.issued_at)}",
            body_style,
        ),
        Paragraph(
            f"<b>Period</b><br/>{period}" if is_proposal else f"<b>Due date</b><br/>{_fmt_date(invoice.due_date)}",
            body_style,
        ),
        Paragraph(
            f"<b>Payment terms</b><br/>{invoice.payment_terms_days} days",
            body_style,
        ),
    ]
    meta = Table([meta_cells], colWidths=[58 * mm, 58 * mm, 58 * mm])
    meta.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ACCENT_LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(meta)
    story.append(Spacer(1, 10 * mm))

    # Seller / buyer
    seller_html = (
        f"<b>{invoice.seller_name or 'Seller'}</b><br/>{_address_html(invoice.seller_address)}"
    )
    if invoice.seller_vat_id:
        seller_html += f"<br/><br/>VAT&nbsp;{invoice.seller_vat_id}"
    if invoice.seller_bank_account:
        seller_html += f"<br/>IBAN&nbsp;{invoice.seller_bank_account}"

    buyer_html = f"<b>{invoice.customer_name}</b><br/>{_address_html(invoice.buyer_address)}"
    if invoice.buyer_vat_id:
        buyer_html += f"<br/><br/>VAT&nbsp;{invoice.buyer_vat_id}"

    party_data = [
        [Paragraph("FROM", label_style), Paragraph("BILL TO", label_style)],
        [Paragraph(seller_html, body_style), Paragraph(buyer_html, body_style)],
    ]
    party_table = Table(party_data, colWidths=[87 * mm, 87 * mm])
    party_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f8fafc")),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(party_table)
    story.append(Spacer(1, 10 * mm))

    if invoice.project_name:
        story.append(Paragraph(f"<b>Project:</b> {invoice.project_name}", muted_style))
        story.append(Spacer(1, 4 * mm))
    if invoice.description:
        story.append(Paragraph(invoice.description, body_style))
        story.append(Spacer(1, 6 * mm))

    # Line items
    table_data = [
        [
            Paragraph("<b>Description</b>", body_style),
            Paragraph("<b>Qty</b>", right_style),
            Paragraph("<b>Unit</b>", right_style),
            Paragraph("<b>Rate</b>" if is_proposal else "<b>Price</b>", right_style),
            Paragraph("<b>Amount</b>", right_style),
        ]
    ]
    for line in lines:
        unit = "hour" if line.unit == "hour" else "—"
        table_data.append(
            [
                Paragraph(line.description, body_style),
                Paragraph(f"{line.quantity:g}", right_style),
                Paragraph(unit, right_style),
                Paragraph(_money(line.unit_price_eur), right_style),
                Paragraph(_money(line.amount_eur), right_style),
            ]
        )

    line_count = len(lines)
    totals_start = line_count + 1
    total_label = "Total (incl. VAT)" if is_proposal else "Total due"
    table_data.extend(
        [
            ["", "", "", Paragraph("<b>Amount (ex VAT)</b>" if is_proposal else "<b>Subtotal</b>", right_style), Paragraph(_money(invoice.subtotal_eur), right_style)],
            [
                "",
                "",
                "",
                Paragraph(f"<b>VAT ({invoice.vat_rate:g}%)</b>", right_style),
                Paragraph(_money(invoice.vat_eur), right_style),
            ],
            [
                "",
                "",
                "",
                Paragraph(f"<b>{total_label}</b>", right_style),
                Paragraph(f"<b>{_money(invoice.amount_eur)}</b>", right_style),
            ],
        ]
    )

    col_widths = [78 * mm, 16 * mm, 18 * mm, 30 * mm, 32 * mm]
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, totals_start - 1), [colors.white, colors.HexColor("#f8fafc")]),
                ("LINEABOVE", (0, totals_start), (-1, totals_start), 1, BORDER),
                ("BACKGROUND", (0, totals_start), (-1, -1), colors.HexColor("#f1f5f9")),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
            ]
        )
    )
    story.append(tbl)
    story.append(Spacer(1, 12 * mm))

    if is_proposal:
        footer = (
            f"This factuurvoorstel totals <b>{_money(invoice.amount_eur)}</b> "
            f"(amount €{_money(invoice.subtotal_eur).lstrip('€')} + VAT €{_money(invoice.vat_eur).lstrip('€')}). "
            f"Reference <b>{invoice.invoice_number}</b>."
        )
    else:
        footer = (
            f"Please transfer <b>{_money(invoice.amount_eur)}</b> before <b>{_fmt_date(invoice.due_date)}</b>"
        )
        if invoice.seller_bank_account:
            footer += f" to IBAN <b>{invoice.seller_bank_account}</b>"
        footer += f", referencing invoice <b>{invoice.invoice_number}</b>."
    story.append(Paragraph(footer, muted_style))

    doc.build(story)

    rel = path.relative_to(Path(settings.archive_root))
    relative = str(rel).replace("\\", "/")
    try:
        from app.services.object_store import upload_pdf

        upload_pdf(path, relative)
    except Exception:
        # Local archive remains the source of truth if object store is down.
        pass
    return relative


def resolve_pdf_absolute(relative_path: str) -> Path:
    return Path(settings.archive_root) / relative_path


def load_pdf_bytes(relative_path: str) -> bytes | None:
    """Prefer object store when configured, else local archive PVC."""
    try:
        from app.services.object_store import download_pdf_bytes, object_key_for_pdf, object_store_enabled

        if object_store_enabled():
            data = download_pdf_bytes(object_key_for_pdf(relative_path))
            if data:
                return data
    except Exception:
        pass
    path = resolve_pdf_absolute(relative_path)
    if path.is_file():
        return path.read_bytes()
    return None
