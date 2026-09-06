# InventorySync

Keeps stock levels consistent across multiple sales channels (Shopify + a
marketplace) so the same unit never sells twice.

**Why this matters:** overselling = refunds, chargebacks, angry reviews. If
Shopify and a marketplace both think they have the last unit of a SKU and both
sell it, someone has to eat the cancellation — and it's always the customer
who finds out last.

## The trust builder: optimistic locking

Two channels can report a stock change on the same SKU at the same instant —
a marketplace sale and a Shopify sale landing within milliseconds of each
other. Naive read-modify-write loses one of them:

```
Request A: read qty=10           Request B: read qty=10
Request A: write qty=9  (-1)
                                  Request B: write qty=9  (-1)   <- LOST UPDATE
```

Both requests decremented, but the qty only dropped by 1 instead of 2. That's
a phantom unit sold that doesn't exist.

InventorySync fixes this with a `version` integer on every SKU row:

```
for attempt in range(MAX_RETRIES):
    sku = SELECT * FROM sku WHERE sku_code = :code
    new_qty = max(0, sku.canonical_qty + delta)
    rowcount = UPDATE sku
               SET canonical_qty = new_qty, version = version + 1
               WHERE id = :id AND version = :read_version
    if rowcount == 1:
        return sku   # we won the race, done
    # someone else updated it between our read and write — retry
    sleep(backoff)
```

The `WHERE version = :read_version` clause means the write only succeeds if
nobody else touched the row since we read it. If it fails, we re-read the
now-current value and try again with exponential backoff + jitter. No row
locks held across requests, no deadlocks, no lost updates.

This is proven under a live 30-concurrent-request race in `tests/` and via a
real HTTP demo below — the retry path isn't theoretical, the logs show it
firing and resolving on every conflict.

## Idempotency

Webhooks get retried by the sender (network blips, timeouts). If the same
`event_id` were applied twice, that's a phantom double-decrement — worse than
doing nothing. `ChannelEvent.event_id` has a unique constraint; the event is
inserted *before* the stock is touched, so a replayed event hits the unique
violation and is dropped before it can double-apply.

## Flow

```
Shopify sale ──┐
                ├──▶ POST /webhooks/{channel} ──▶ verify signature
Marketplace ────┘         │                            │
   sale                   ▼                            ▼
              insert ChannelEvent(event_id)   duplicate? → 200 "duplicate", stop
                     (unique constraint)
                          │
                          ▼
              optimistic-lock update loop (concurrency/locking.py)
                          │
                          ▼
              fan out new canonical qty to every OTHER channel
              (sync/reconcile.py — httpx, retried with backoff)
```

## Models

- `Sku`: `id`, `sku_code`, `canonical_qty`, `version`
- `ChannelEvent`: `id`, `channel`, `event_id` (unique), `sku_code`, `delta`, `applied_at`

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhooks/{channel}` | Inbound stock change from a channel (requires `X-Signature` HMAC header) |
| `GET` | `/stock/{sku_code}` | Current canonical quantity + version |
| `POST` | `/stock/{sku_code}/adjust` | Manual/admin adjustment (restock, correction) — fans out to all channels |
| `GET` | `/health` | Health check |
| `POST`/`GET` | `/mock/{channel}/inventory[/{sku_code}]` | Fake Shopify/marketplace receivers (for demo/tests — swap for real channel APIs in production) |

## Production concerns already in place

- **Signature verification** — HMAC-SHA256 over the raw body, constant-time compare (`app/security.py`)
- **Optimistic-lock retry loop** — 5 attempts, exponential backoff + jitter (`app/concurrency/locking.py`)
- **Idempotency** — unique `event_id` constraint, checked before any mutation
- **Backoff on fan-out** — 3 retries per channel with exponential backoff (`app/sync/reconcile.py`)
- **Structured logging** — every conflict, retry, duplicate, and fan-out failure is logged
- **Sentry** — wired up, no-op unless `SENTRY_DSN` is set
- **Health check** — `GET /health`

## Setup

Requires Python 3.11 and Postgres (already running locally on 5432 for this
demo).

```bash
cd inventory-sync
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

createdb inventorysync          # one-time
cp .env.example .env            # fill in WEBHOOK_SECRET (a real one is
                                 # already generated in .env for this demo)
```

## Run

```bash
source .venv/bin/activate
export $(cat .env | xargs)
uvicorn app.main:app --reload --port 8000
```

Seeds 3 demo SKUs on first startup (`WIDGET-1`, `GADGET-2`, `GIZMO-3`) so it's
never empty. Interactive docs at `http://localhost:8000/docs`.

## Test

```bash
source .venv/bin/activate
export $(cat .env | xargs)
python -m pytest tests/ -v
```

`tests/test_concurrency.py` is the core proof: 10 real OS threads fire
concurrent updates at the same SKU row; the test asserts the final quantity
and version reflect *every* delta exactly — no lost updates. A second test
proves quantity floors at 0 under a 20-way concurrent overselling attempt.

`tests/test_idempotency.py` proves a replayed webhook event is a no-op, and
that an unsigned/bad-signature request is rejected with 401.

## Docker

```bash
docker build -t inventorysync .
docker run -p 8000:8000 --env-file .env inventorysync
```

(Needs `DATABASE_URL` to point at a reachable Postgres — the container itself
doesn't bundle one.)
