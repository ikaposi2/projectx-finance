from pydantic import BaseModel, Field


class CompensationRow(BaseModel):
    partner_id: str
    partner_name: str = ""
    billable_hours: float
    chargeback_hours: float
    chargeback_eur: float


class CompensationEffectOut(BaseModel):
    time_entry_id: str
    partner_id: str
    partner_name: str
    project_id: str | None = None
    classification: str
    hours: float
    rate_eur: float
    amount_eur: float
    can_undo: bool = True
    updated_at: str | None = None


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


class CompanyProfileOut(BaseModel):
    legal_name: str
    address_line1: str | None = None
    address_line2: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country: str | None = None
    vat_id: str | None = None
    coc_number: str | None = None
    bank_account: str | None = None
    invoice_email: str | None = None
    payment_terms_days: int = 30


class CompanyProfileUpdate(BaseModel):
    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    postal_code: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    vat_id: str | None = Field(default=None, max_length=64)
    coc_number: str | None = Field(default=None, max_length=64)
    bank_account: str | None = Field(default=None, max_length=64)
    invoice_email: str | None = Field(default=None, max_length=320)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)


class InvoiceLineOut(BaseModel):
    id: str
    description: str
    quantity: float
    unit: str
    unit_price_eur: float
    amount_eur: float
    source: str
    time_entry_id: str | None = None


class InvoiceOut(BaseModel):
    id: str
    invoice_number: str
    kind: str
    project_id: str | None
    project_name: str
    customer_id: str | None
    customer_name: str
    buyer_vat_id: str | None = None
    buyer_address: str | None = None
    seller_name: str = ""
    seller_vat_id: str | None = None
    seller_address: str | None = None
    seller_bank_account: str | None = None
    description: str | None = None
    period_label: str | None = None
    subtotal_eur: float = 0
    vat_rate: float = 21
    vat_eur: float = 0
    amount_eur: float
    payment_terms_days: int = 30
    status: str
    notes: str | None
    lines: list[InvoiceLineOut] = Field(default_factory=list)


class InvoiceUpdate(BaseModel):
    status: str = Field(pattern="^(draft|issued|paid)$")


class InvoiceGenerate(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    kind: str = Field(pattern="^(fixed_completion|fixed_milestone_50|tm_hours)$")
    description: str | None = Field(default=None, max_length=1000)
    period_label: str | None = Field(default=None, max_length=80)


class BillingAction(BaseModel):
    kind: str
    label: str
    amount_eur: float
    enabled: bool
    hours: float | None = None
    rate_eur: float | None = None


class BillingCandidate(BaseModel):
    project_id: str
    project_name: str
    customer_id: str | None = None
    customer_name: str
    fixed_price_eur: float
    progress: str
    report_url: str | None = None
    actions: list[BillingAction]
