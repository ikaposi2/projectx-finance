# projectX-finance

Finance microservice: **customer billing** (invoices), plus internal partner chargeback / company reserve.

- **Auth:** JWT issued by `projectX-identity` (shared `JWT_SECRET`)
- **DB:** logical database `finance` on in-cluster Postgres
- **Events:** durable consumer on `projectx.events.time.>` (`PROJECTX_EVENTS`)
- **Upstream:** project, customer, time, partner (billing, reporting, personnel)

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
| `TIME_SERVICE_URL` | | Time entries (T&M month filter, reporting, refuse undo) |
| `PARTNER_SERVICE_URL` | | Resources (personnel proposals, reporting capacity) |
| `COMPANY_*` | | Seller defaults for company profile bootstrap |
| `ARCHIVE_ROOT` | `/var/archive` | Invoice PDF archive (PVC in cluster) |
| `CORS_ORIGINS` | localhost Vite | Browser origins |

## API (behind `/api/finance` in cluster)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/health` | no | Liveness |
| GET | `/company` | manager+ | Seller profile (auto-created) |
| PATCH | `/company` | manager+ | Update seller details |
| GET | `/billing/candidates?month=YYYY-MM` | manager+ | Projects ready to invoice + actions (T&M scoped to month; default current UTC month) |
| GET | `/reports/summary?from=&to=` | manager+ | Five management figures (funnel, WIP, utilization, delivered, received) |
| POST | `/invoices/generate` | manager+ | Create draft from project + kind (`period_label` required for `tm_hours`) |
| GET | `/invoices` | manager+ | List invoices (with lines) |
| GET | `/invoices/archive?year=&quarter=` | manager+ | Collect invoices for a quarter or full year (issue date) |
| PATCH | `/invoices/{id}` | manager+ | `draft` → `issued` (send) → `paid` \| `returned`; `paid` → `issued` |
| DELETE | `/invoices/{id}` | manager+ | Remove `draft` or `returned` invoice (unlocks hours) |
| GET | `/invoices/{id}/pdf` | manager+ | Download archived PDF (generated on send) |
| GET | `/invoices/agenda?week_start=` | manager+ | Due dates for ISO week + overdue issued invoices |
| GET | `/compensation` | manager+ | Applied ledger entries with partner names |
| POST | `/compensation/{time_entry_id}/undo` | manager+ | Refuse related time entry + reverse ledger |
| GET | `/reserve` | manager+ | Reserve from **net** revenue (ex VAT) vs target |
| GET | `/vat` | manager+ | Separate VAT account by calendar quarter |
| POST | `/vat/remit` | manager+ | Record quarterly VAT remittance to tax authority |
| GET | `/costs?month=YYYY-MM` | manager+ | Costs that apply to that month (omit `month` for all definitions) |
| POST | `/costs` | manager+ | Create one-off or recurring monthly cost |
| PATCH | `/costs/{id}` | manager+ | Update cost / invoice matched+paid flags |
| DELETE | `/costs/{id}` | manager+ | Remove a monthly cost |

### Invoice kinds

| Kind | When |
|------|------|
| `fixed_milestone_50` | Fixed price **above** `MILESTONE_THRESHOLD_EUR`, funnel `in_delivery` or `delivered`, no prior milestone draft/issued/paid — amount = 50% of assignment (capped by remaining) |
| `fixed_completion` | Funnel `delivered`, remaining fixed amount after prior invoices |
| `tm_hours` | **Time & material only** (no fixed price) — unbilled approved hours in `period_label` (YYYY-MM) × staffing rates; available while funnel is `in_delivery` or `delivered` |

Fixed-price projects below the milestone threshold are invoiced only via final completion — not as separate hour invoices. Above threshold: optional 50% milestone, then final for the remainder.

Completed projects (`progress=complete`) are excluded from hour booking (`GET /projects/bookable`); Finance still sees them via `include_complete=true` for the final invoice.

Invoices snapshot seller (company) and buyer (customer bill-to / MSP parent), VAT, multiline buyer address, and line items.

Each invoice gets a unique number at creation: `INV-{year}-{seq}` (e.g. `INV-2026-0001`), unique per tenant.

On **send** (`draft` → `issued`): sets `issued_at`, `due_date` (issue + payment terms), generates a PDF under `ARCHIVE_ROOT`, and **locks** linked time entries. **Returned** invoices unlock hours again; delete returned or draft invoices via `DELETE`. Only `issued` and `paid` invoices count toward billed amounts and hour locks.

## Event handling

| Event | Effect |
|-------|--------|
| `TimeEntryApproved` (`billable`) | Record hours on partner compensation (no € yet) |
| `TimeEntryApproved` (`approved_non_billable`) | Chargeback hours × `INTERNAL_RATE_EUR` |
| `TimeEntryRefused` / `TimeEntryReset` | Mark effect unapplied |

Reserve estimate ≈ issued+paid **net** invoice totals − chargeback euros (VAT excluded — see VAT account).

VAT on issued/paid invoices accumulates in a separate account by calendar quarter and is remitted every 3 months via `POST /vat/remit`.

See also: [delivery lifecycle](../projectX-docs/docs/architecture/delivery-lifecycle.md) (fixed vs T&M flows).

## Out of scope (v1)

- Bank account linking / automatic payment detection (deferred)
- Email delivery of invoices
- Year-end 50/50 bonus workflow
