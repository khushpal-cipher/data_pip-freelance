# RefundReconciler

**Refunds that never sync back to the books = overstated revenue = wrong taxes.** A
customer gets refunded in Stripe or Shopify, the money leaves the bank account, and if
that refund doesn't land in QuickBooks the books still show the original sale as
untouched revenue. RefundReconciler catches every refund webhook, locates the original
transaction, computes the exact partial-refund portion, stamps the FX rate at the moment
of refund (never re-derived later), and posts a RefundReceipt to QuickBooks — with the
same duplicate-proof idempotency guarantee as [SyncGuard](../SyncGuard), so a webhook
storm never posts the same refund twice.

## How it flows

```
Stripe/Shopify webhook
        │  verify signature (HMAC, timing-safe, timestamp tolerance)
        ▼
   ledger.enqueue()  ── UNIQUE(refund_id) ──▶ duplicate delivery? → no-op, HTTP 200
        │  new refund only
        ▼
  Celery task (Redis-backed queue)
        │
        ├─ matching/locate.py   → find the original transaction by stored source_id
        ├─ currency/convert.py  → refund_portion = refunded / original (Decimal, exact)
        │                          over-refund guard: cumulative refunds ≤ original
        │                          FX: base_amount = amount × fx_rate (stamped AT REFUND TIME)
        ▼
   QuickBooks RefundReceipt created
        │
   failure → exponential backoff → retry → after N attempts: dead-lettered
        │                                          │
        ▼                                          ▼
   ledger marked SYNCED                 POST /reconcile/replay/{refund_id}
```

## Why partial refunds and FX are the trust builder

Two things break naive refund integrations, silently:

1. **Partial refunds.** A $30 refund on a $100 order isn't "a refund" — it's 30% of it.
   Get the portion math wrong (or use floats and let rounding drift across dozens of
   partial refunds) and the tax line, the COGS allocation, everything downstream is off.
   RefundReconciler computes `refund_portion = refunded_amount / original_amount` in
   `Decimal`, guards the cumulative total against ever exceeding the original, and holds
   — rather than silently caps — anything that would over-refund.

2. **Multi-currency FX.** A EUR sale gets refunded weeks later, after the exchange rate
   moved. Re-deriving "today's rate" at refund time silently changes the book value of a
   sale that already closed. RefundReconciler stamps the FX rate **once**, at the moment
   the refund event arrives, onto the ledger row — and every downstream calculation reads
   that stamped rate forever. The difference between the refund's rate and the original
   sale's booking rate is posted as its own realized FX gain/loss line, never netted
   silently into revenue.

Both are covered by the demo below, end to end, with assertions.

## What it looks like running

Reproduce it locally in under a second — no Docker, no credentials, no network:

```bash
python demo.py            # 7 scenes against a real ledger, Celery in eager mode
python seed_dashboard.py  # fills the dashboard with sample activity
```

<details>
<summary><b>Full <code>demo.py</code> output</b> — duplicate storm, forged webhook, partial + over-refund, multi-currency FX, unmatched refund, outage → dead-letter → replay</summary>

```
RefundReconciler — Stripe/Shopify → QuickBooks Online refund sync
Live run against a real SQLite ledger, Celery in eager mode, and a stubbed QuickBooks. No network calls.

──────────────────────────────────────────────────────────────────────────────
SCENE 1 — Duplicate webhook storm
Stripe resends a webhook on any slow or non-200 response.

  delivery 1: HTTP 200  already_queued=False
  delivery 2: HTTP 200  already_queued=True
  delivery 3: HTTP 200  already_queued=True
  delivery 4: HTTP 200  already_queued=True
  delivery 5: HTTP 200  already_queued=True
  ✓ 5 identical deliveries → 1 ledger row → 1 QuickBooks RefundReceipt
  ✓ enforced by UNIQUE(refund_id) in the database, not by an if-statement

──────────────────────────────────────────────────────────────────────────────
SCENE 2 — Forged webhook
Your endpoint is public. Anything that can POST to it can invent a refund.

  ✓ wrong signature → HTTP 401, rejected before any database write

──────────────────────────────────────────────────────────────────────────────
SCENE 3 — Partial refunds, then an over-refund
Two partial refunds against one order, then one that would over-refund it.

  re_2001a  30.00  portion=0.300000
  re_2001b  40.00  portion=0.400000
  ✓ 70.00 of 100.00 refunded as two QBO RefundReceipts, each with the correct portion
  re_2001c  50.00  status=over_refund  error=refund 50.0000 + already_refunded 70.0000 exceeds original total 100.0000
  ✗ third refund of 50.00 rejected: refund 50.0000 + already_refunded 70.0000 exceeds original total 100.0000
  ✓ over-refund never reaches QuickBooks; it is held on the ledger for a human

──────────────────────────────────────────────────────────────────────────────
SCENE 4 — Multi-currency refund with FX rate stamped at refund time
The original sale was booked at one FX rate; the refund happens later at a different rate.

  refund 100.00 EUR, booked at 1.00, refunded at fx_rate=1.15000000
  portion=0.500000 base_amount=115.00 fx_gain_loss=15.00 original_txn=ch_3001
  ✓ FX rate recorded on the refund itself — refunds computed from it never drift
  ✓ realized FX gain/loss (15.00) posted as its own line, not netted into revenue

──────────────────────────────────────────────────────────────────────────────
SCENE 5 — Unmatched refund
A refund event references an order we never booked. Guessing here is how books go quietly wrong.

  refund_id=re_orphan  original_txn=ch_never_seen  status=unmatched
  ✗ held: no original transaction found for stripe:ch_never_seen
  ✓ unmatched refund is held, not silently dropped or guessed at

──────────────────────────────────────────────────────────────────────────────
SCENE 6 — QuickBooks outage → dead letter → replay
An outage must not silently drop a refund.

  attempts=6  status=failed  error=QBO returned 503 Service Unavailable
  ✗ dead-lettered after 6 attempts: QBO returned 503 Service Unavailable
  POST /reconcile/replay/stripe:re_4001 -> 200
  status=synced  dest_id=qbo-refund-5
  ✓ replay after the outage clears re-runs the same refund_id and it syncs — never a duplicate

──────────────────────────────────────────────────────────────────────────────
SCENE 7 — Reconciliation status endpoint
The owner-facing snapshot of what's healthy and what needs a human.

  {"last_sync": "...", "synced_today": 5, "pending": 0, "failed": 0, "unmatched": 1, "over_refund": 1}
  ✓ GET /reconcile/status returns synced/pending/failed/unmatched/over_refund counts

──────────────────────────────────────────────────────────────────────────────
All scenes passed. 5 refund receipts posted to (fake) QuickBooks.
```

</details>

## Idempotency ledger

Same pattern as SyncGuard: `RefundRecord.refund_id` is `UNIQUE` in Postgres. Every insert
attempt is caught and folded into a lookup on conflict — the no-duplicate guarantee lives
in the database schema, not in an `if refund_id in seen` check that a second process could
race past. `tests/test_ledger_idempotency.py` and Scene 1 of the demo both prove five
identical webhook deliveries produce exactly one processed refund.

## API

| Endpoint | Purpose |
|---|---|
| `POST /webhooks/stripe` | Stripe refund events (`charge.refunded`, `refund.*`). HMAC-SHA256 over `{timestamp}.{body}`, timestamp tolerance window. |
| `POST /webhooks/shopify` | Shopify refund events (`refunds/create`). Base64 HMAC-SHA256 over the raw body. |
| `GET /reconcile/status` | Counts by status: synced today, pending, failed, unmatched, over_refund. |
| `POST /reconcile/replay/{refund_id}` | Reset a dead-lettered (or any) ledger row to `pending` and re-enqueue it. |
| `GET /dashboard` | Owner-facing HTML status page. |
| `GET /health` | Checks DB and broker connectivity. |

## Production hardening already in place

- **Signature verification** per provider (Stripe timestamp+HMAC with replay tolerance,
  Shopify base64 HMAC) — an unsigned or tampered request never reaches the database.
- **Celery retry policy** — exponential backoff (`retry_backoff_base_seconds`, capped at
  `retry_backoff_max_seconds`), bounded by `max_task_attempts`.
- **Dead-letter** — after the attempt cap, the ledger row is marked `FAILED` and stays
  there for a human; `POST /reconcile/replay/{id}` clears it back to `pending`.
- **Redacted logging** — `app/logging.py` strips webhook secrets, tokens, and anything
  matching `secret|token|password|authoriz` from every structured log line before it hits
  stdout (and therefore before it would hit Sentry).
- **Sentry** — wired via `SENTRY_DSN`; a no-op if unset.
- **Health check** — `GET /health` verifies both the Postgres connection and the Redis
  broker before returning 200.

## Setup

Requires Postgres and Redis running locally (or use `docker compose up`).

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

createdb refund_reconciler          # or point DATABASE_URL at an existing one
cp .env.example .env                # fill in real Stripe/Shopify/QBO creds when you have them

python demo.py                      # proves the core logic with zero infra
python seed_dashboard.py            # optional: sample data for /dashboard

uvicorn app.main:app --reload                          # terminal 1 — API
celery -A app.celery_app worker --loglevel=info -Q refunds   # terminal 2 — worker
```

Then visit `http://localhost:8000/dashboard`, or `docker compose up` to run Postgres +
Redis + web + worker together.

### Tests

```bash
pytest -q   # 28 tests: refund-portion math, over-refund guard, FX stamping, idempotency,
            # signature verification, and a full webhook→ledger→task→QBO flow per scenario
```
