import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, func
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
    classification: Mapped[str] = mapped_column(String(40))  # billable | approved_non_billable
    hours: Mapped[float] = mapped_column(Float)
    rate_eur: Mapped[float] = mapped_column(Float, default=0.0)
    amount_eur: Mapped[float] = mapped_column(Float, default=0.0)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class Invoice(Base):
    """Manual / event-driven invoices (draft → issued → paid)."""

    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    amount_eur: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(40), default="draft")  # draft | issued | paid
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
