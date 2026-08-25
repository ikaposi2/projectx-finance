import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CompensationEffect(Base):
    """Idempotent ledger of approved time entries that affect compensation / chargeback."""

    __tablename__ = "compensation_effects"

    time_entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    partner_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    classification: Mapped[str] = mapped_column(String(40))
    hours: Mapped[float] = mapped_column(Float)
    rate_eur: Mapped[float] = mapped_column(Float, default=0.0)
    amount_eur: Mapped[float] = mapped_column(Float, default=0.0)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class CompanyProfile(Base):
    """Seller (our company) details printed on invoices — one row per tenant."""

    __tablename__ = "company_profiles"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(200), default="")
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    country: Mapped[str | None] = mapped_column(String(120), nullable=True)
    vat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coc_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class Invoice(Base):
    """Customer invoice (draft → issued → paid)."""

    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    invoice_number: Mapped[str] = mapped_column(String(40), index=True, default="")
    kind: Mapped[str] = mapped_column(String(40), default="manual")
    # fixed_completion | fixed_milestone_50 | tm_hours | manual
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    project_name: Mapped[str] = mapped_column(String(200), default="")
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    # Buyer snapshot
    buyer_vat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    buyer_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Seller snapshot
    seller_name: Mapped[str] = mapped_column(String(200), default="")
    seller_vat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seller_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seller_bank_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    subtotal_eur: Mapped[float] = mapped_column(Float, default=0.0)
    vat_rate: Mapped[float] = mapped_column(Float, default=21.0)
    vat_eur: Mapped[float] = mapped_column(Float, default=0.0)
    amount_eur: Mapped[float] = mapped_column(Float, default=0.0)  # total incl VAT
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(40), default="draft")
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    invoice_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    description: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    unit: Mapped[str] = mapped_column(String(20), default="lump")  # hour | lump
    unit_price_eur: Mapped[float] = mapped_column(Float, default=0.0)
    amount_eur: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    time_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class VatRemittance(Base):
    """Quarterly VAT payment to the tax authority (every 3 months)."""

    __tablename__ = "vat_remittances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    year: Mapped[int] = mapped_column(Integer)
    quarter: Mapped[int] = mapped_column(Integer)  # 1..4
    amount_eur: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
