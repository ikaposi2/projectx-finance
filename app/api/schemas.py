from pydantic import BaseModel, Field


class CompensationRow(BaseModel):
    partner_id: str
    billable_hours: float
    chargeback_hours: float
    chargeback_eur: float


class ReserveSnapshot(BaseModel):
    target_eur: float
    current_reserve_eur: float
    surplus_eur: float
    internal_rate_eur: float
    chargeback_hours: float
    chargeback_eur: float
    billable_hours: float
    invoice_draft_eur: float
    invoice_issued_eur: float
    invoice_paid_eur: float


class InvoiceCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    amount_eur: float = Field(ge=0, le=10_000_000)
    project_id: str | None = Field(default=None, max_length=36)
    customer_id: str | None = Field(default=None, max_length=36)
    notes: str | None = Field(default=None, max_length=500)


class InvoiceUpdate(BaseModel):
    status: str = Field(pattern="^(draft|issued|paid)$")


class InvoiceOut(BaseModel):
    id: str
    project_id: str | None
    customer_id: str | None
    customer_name: str
    amount_eur: float
    status: str
    notes: str | None
