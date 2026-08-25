# projectX-finance

Finance microservice: **customer billing** (invoices), plus internal partner chargeback / company reserve.

- **Auth:** JWT issued by `projectX-identity` (shared `JWT_SECRET`)
- **DB:** logical database `finance` on in-cluster Postgres
- **Events:** durable consumer on `projectx.events.time.>` (`PROJECTX_EVENTS`)
- **Upstream:** project + customer services for invoice generation

## Local run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8007
```

## Env

| Variable | Default | Purpose |
|----------|---------|---------|
| `PX_DB_*` | local/dev | Postgres (`PX_DB_NAME=finance`) |
| `JWT_SECRET` | must match identity | Validate access tokens |
| `NATS_URL` | `nats://nats:4222` | JetStream (empty = skip consumer) |
| `NATS_CONSUMER` | `finance-time-entries` | Durable pull consumer |
| `INTERNAL_RATE_EUR` | `75` | Non-billable chargeback rate |
| `RESERVE_TARGET_EUR` | `50000` | Company reserve target |
| `MILESTONE_THRESHOLD_EUR` | `30000` | Fixed-price assignments above this can take a 50% milestone invoice |
| `PROJECT_SERVICE_URL` | | Fetch project progress / staffing |
| `CUSTOMER_SERVICE_URL` | | Buyer / bill-to details |
| `IDENTITY_SERVICE_URL` | | Resolve partner display names |
| `TIME_SERVICE_URL` | | Undo compensation via refuse |
| `COMPANY_*` | | Seller defaults for company profile bootstrap |
| `CORS_ORIGINS` | localhost Vite | Browser origins |

## API (behind `/api/finance` in cluster)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/health` | no | Liveness |
| GET | `/company` | manager+ | Seller profile (auto-created) |
| PATCH | `/company` | manager+ | Update seller details |
| GET | `/billing/candidates` | manager+ | Projects ready to invoice + actions |
| POST | `/invoices/generate` | manager+ | Create draft from project + kind |
| GET | `/invoices` | manager+ | List invoices (with lines) |
| PATCH | `/invoices/{id}` | manager+ | `draft` → `issued` → `paid` |
| GET | `/compensation` | manager+ | Applied ledger entries with partner names |
| POST | `/compensation/{time_entry_id}/undo` | manager+ | Refuse related time entry + reverse ledger |
| GET | `/reserve` | manager+ | Reserve from **net** revenue (ex VAT) vs target |
| GET | `/vat` | manager+ | Separate VAT account by calendar quarter |
| POST | `/vat/remit` | manager+ | Record quarterly VAT remittance to tax authority |

### Invoice kinds

| Kind | When |
|------|------|
| `fixed_milestone_50` | Fixed price &gt; threshold, progress ≥ 50%, no prior milestone invoice |
| `fixed_completion` | Progress `complete`, remaining fixed amount after prior invoices |
| `tm_hours` | Unbilled approved billable hours × project staffing rates |

Invoices snapshot seller (company) and buyer (customer bill-to / MSP parent), VAT, and line items.

## Event handling

| Event | Effect |
|-------|--------|
| `TimeEntryApproved` (`billable`) | Record hours on partner compensation (no € yet) |
| `TimeEntryApproved` (`approved_non_billable`) | Chargeback hours × `INTERNAL_RATE_EUR` |
| `TimeEntryRefused` / `TimeEntryReset` | Mark effect unapplied |

Reserve estimate ≈ issued+paid **net** invoice totals − chargeback euros (VAT excluded — see VAT account).

VAT on issued/paid invoices accumulates in a separate account by calendar quarter and is remitted every 3 months via `POST /vat/remit`.

## Out of scope (v1)

- PDF/email invoicing
- Year-end 50/50 bonus workflow
- Month-scoped T&M batching UI
