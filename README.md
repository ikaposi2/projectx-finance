# projectX-finance

Finance microservice: partner compensation ledger (incl. €75/h non-billable chargeback), company reserve snapshot, and draft invoices.

- **Auth:** JWT issued by `projectX-identity` (shared `JWT_SECRET`)
- **DB:** logical database `finance` on in-cluster Postgres
- **Events:** durable consumer on `projectx.events.time.>` (`PROJECTX_EVENTS`)

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
| `CORS_ORIGINS` | localhost Vite | Browser origins |

## API (behind `/api/finance` in cluster)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/health` | no | Liveness |
| GET | `/compensation` | manager+ | Per-partner billable hours + chargeback € |
| GET | `/reserve` | manager+ | Reserve target vs approximate current |
| GET | `/invoices` | manager+ | List invoices |
| POST | `/invoices` | manager+ | Create draft invoice |
| PATCH | `/invoices/{id}` | manager+ | `draft` → `issued` → `paid` |

## Event handling

| Event | Effect |
|-------|--------|
| `TimeEntryApproved` (`billable`) | Record hours on partner compensation (no € yet) |
| `TimeEntryApproved` (`approved_non_billable`) | Chargeback hours × `INTERNAL_RATE_EUR` |
| `TimeEntryRefused` / `TimeEntryReset` | Mark effect unapplied |

Reserve estimate ≈ issued+paid invoice totals − chargeback euros (proxy until full P&L).

## Out of scope (v1)

- PDF/email invoicing
- Year-end 50/50 bonus workflow
- Billable euro rates from project staffing
