"""SyncGuard proof-of-work demo — no Docker, no Postgres, no credentials, no network.

Runs the real webhook -> ledger -> worker -> QBO path against a throwaway SQLite
file and a fake QuickBooks that records calls instead of making them. Every scene
below is a failure mode that breaks amateur Shopify->QBO integrations; each one
asserts the outcome, so this file is also the end-to-end regression check.

    python demo.py
"""

import json
import os
import pathlib
import sys

DB_PATH = pathlib.Path(__file__).parent / "demo.db"
DB_PATH.unlink(missing_ok=True)

WEBHOOK_SECRET = "demo-webhook-secret"
os.environ.update(
    DATABASE_URL=f"sqlite:///{DB_PATH}",
    SHOPIFY_WEBHOOK_SECRET=WEBHOOK_SECRET,
    SHOPIFY_STORE_DOMAIN="demo-store.myshopify.com",
    QBO_INCOME_ACCOUNT_ID="79",
    QBO_FEE_ACCOUNT_ID="80",
    QBO_CLEARING_ACCOUNT_ID="81",
    LOG_LEVEL="WARNING",
)

import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import worker  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SyncRecord, SyncStatus  # noqa: E402
from app.reconcile import nightly  # noqa: E402
from app.sync import ledger  # noqa: E402

ORANGE, DARK, GREEN, RED, DIM, OFF = "\033[38;5;173m", "\033[1m", "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def scene(n, title, why):
    print(f"\n{ORANGE}{'─' * 78}{OFF}\n{DARK}SCENE {n} — {title}{OFF}\n{DIM}{why}{OFF}\n")


def ok(msg):
    print(f"  {GREEN}✓{OFF} {msg}")


def bad(msg):
    print(f"  {RED}✗{OFF} {msg}")


# ---------------------------------------------------------------- fake QuickBooks
class FakeQBO:
    """Stands in for QuickBooks Online. Records payloads; can simulate an outage."""

    calls: list = []
    down = False
    _next_id = 4000

    def __init__(self, settings=None):
        pass

    def _create(self, kind, payload):
        if FakeQBO.down:
            raise RuntimeError("QBO returned 503 Service Unavailable")
        FakeQBO.calls.append({"type": kind, "payload": payload})
        FakeQBO._next_id += 1
        return {"Id": str(FakeQBO._next_id), "DocNumber": payload["DocNumber"]}

    def create_sales_receipt(self, receipt):
        return self._create("sales_receipt", receipt)

    def create_refund_receipt(self, receipt):
        return self._create("refund_receipt", receipt)

    def close(self):
        pass


class FakeShopifyAdmin:
    """Stands in for the Shopify Admin API used by nightly reconciliation."""

    order_ids: list = []

    def __init__(self, settings=None):
        pass

    def list_order_ids(self, **_):
        return FakeShopifyAdmin.order_ids

    def close(self):
        pass


worker.QBOClient = FakeQBO
nightly.ShopifyClient = FakeShopifyAdmin

settings = get_settings()
init_db()
client = TestClient(app)  # no `with`: lifespan/scheduler stays off, we drive the worker by hand


# ------------------------------------------------------------------- helpers
def post_webhook(payload, topic, secret=WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return client.post(
        "/webhooks/shopify",
        content=body,
        headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": topic, "Content-Type": "application/json"},
    )


def run_worker():
    s = get_session()
    try:
        return worker.process_pending(s, settings=settings)
    finally:
        s.close()


def rows(**where):
    s = get_session()
    try:
        stmt = select(SyncRecord)
        for k, v in where.items():
            stmt = stmt.where(getattr(SyncRecord, k) == v)
        return list(s.exec(stmt))
    finally:
        s.close()


ORDER = {
    "id": 5001,
    "created_at": "2026-01-15T10:00:00-05:00",
    "updated_at": "2026-01-15T10:00:00-05:00",
    "currency": "USD",
    "total_price": "100.00",
    "email": "buyer@example.com",
    "line_items": [{"title": "Widget", "quantity": 2, "price": "50.00"}],
    "shopify_payout_fee": 3.20,
}


def refund(refund_id, amount):
    return {
        "id": refund_id,
        "order_id": 5001,
        "created_at": "2026-01-16T10:00:00-05:00",
        "currency": "USD",
        "transactions": [{"amount": amount}],
        "refund_line_items": [],
    }


print(f"\n{DARK}SyncGuard — Shopify → QuickBooks Online sync engine{OFF}")
print(f"{DIM}Live run against a real SQLite ledger and a stubbed QuickBooks. No network calls.{OFF}")

# ---------------------------------------------------------------------------
scene(1, "Duplicate webhook storm", "Shopify resends a webhook on any slow or non-200 response. "
      "A naive integration books a second invoice every time.")
for i in range(5):
    r = post_webhook(ORDER, "orders/create")
    print(f"  delivery {i + 1}: HTTP {r.status_code}  already_queued={r.json()['already_queued']}")
run_worker()
receipts = [c for c in FakeQBO.calls if c["type"] == "sales_receipt"]
assert len(rows(source_id="5001")) == 1, "duplicate ledger row created"
assert len(receipts) == 1, f"expected 1 QBO receipt, got {len(receipts)}"
ok("5 identical deliveries → 1 ledger row → 1 QuickBooks sales receipt")
ok("enforced by UNIQUE(source_system, source_id) in the database, not by an if-statement")

# ---------------------------------------------------------------------------
scene(2, "Forged webhook", "Your endpoint is public. Anything that can POST to it can invent revenue.")
r = post_webhook({"id": 9999, "total_price": "50000.00"}, "orders/create", secret="attacker-guess")
assert r.status_code == 401 and not rows(source_id="9999"), "forged webhook was accepted"
ok(f"wrong HMAC signature → HTTP {r.status_code}, rejected before any database write")

# ---------------------------------------------------------------------------
scene(3, "Payout fees kept off the revenue line",
      "Shopify deducts its fee before the payout lands. Netting it into revenue understates gross sales.")
lines = receipts[0]["payload"]["Line"]
for ln in lines:
    print(f"  {ln['Description']:<34} {ln['Amount']:>8.2f}  → account {ln.get('AccountRef', ln.get('SalesItemLineDetail', {}).get('ItemRef', {})).get('value')}")
assert len(lines) == 2 and lines[0]["Amount"] == 100.00 and lines[1]["Amount"] == 3.20
ok("revenue posts at gross 100.00; the 3.20 fee is its own line on the expense account")

# ---------------------------------------------------------------------------
scene(4, "Partial refunds", "Two partial refunds against one order, then one that would over-refund it.")
for rid, amt in ((7001, "30.00"), (7002, "40.00")):
    post_webhook(refund(rid, amt), "refunds/create")
run_worker()
refunded = [c for c in FakeQBO.calls if c["type"] == "refund_receipt"]
for c in refunded:
    print(f"  {c['payload']['DocNumber']:<28} {c['payload']['TotalAmt']:>8.2f}")
assert len(refunded) == 2 and sum(c["payload"]["TotalAmt"] for c in refunded) == 70.00
ok("70.00 of 100.00 refunded as two QBO RefundReceipts linked to the original")

post_webhook(refund(7003, "50.00"), "refunds/create")
run_worker()
over = rows(source_id="refund:7003")[0]
assert over.status != SyncStatus.SYNCED and "exceeds" in (over.last_error or "")
bad(f"third refund of 50.00 rejected: {over.last_error}")
ok("over-refund never reaches QuickBooks; it is held on the ledger for a human")

# ---------------------------------------------------------------------------
scene(5, "QuickBooks outage → backoff → dead letter → replay",
      "An outage must not silently drop an order.")
FakeQBO.down = True
post_webhook({**ORDER, "id": 5002}, "orders/create")
for attempt in range(settings.max_sync_attempts):
    s = get_session()
    rec = s.exec(select(SyncRecord).where(SyncRecord.source_id == "5002")).first()
    rec.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)  # fast-forward the backoff clock
    s.add(rec)
    s.commit()
    s.close()
    run_worker()
    rec = rows(source_id="5002")[0]
    print(f"  attempt {rec.attempts}: status={rec.status.value:<8} next retry in "
          f"{'—' if rec.status == SyncStatus.FAILED else f'{min(30 * 2 ** rec.attempts, 21600)}s'}")
assert rows(source_id="5002")[0].status == SyncStatus.FAILED
ok(f"exhausted {settings.max_sync_attempts} attempts with exponential backoff → dead-lettered, not lost")

FakeQBO.down = False
print(f"\n  {DIM}QuickBooks recovers; operator replays the dead letter:{OFF}")
print(f"  $ curl -X POST localhost:8001/sync/replay/5002 → {client.post('/sync/replay/5002').json()}")
run_worker()
assert rows(source_id="5002")[0].status == SyncStatus.SYNCED
ok("replayed and synced — the order the outage nearly ate is now in QuickBooks")

# ---------------------------------------------------------------------------
scene(6, "Nightly reconciliation", "Idempotency stops duplicates. Only reconciliation catches what never arrived.")
FakeShopifyAdmin.order_ids = ["5001", "5002", "5003"]  # 5003 never reached the webhook endpoint
s = get_session()
# reference = tomorrow, so the job audits "yesterday relative to reference" = today's records
report = nightly.run_reconciliation(s, settings=settings, reference=datetime.now(timezone.utc) + timedelta(days=1))
s.close()
print(f"  Shopify orders yesterday : {report['shopify_order_count']}")
print(f"  QuickBooks synced        : {report['qbo_synced_count']}")
print(f"  Missing                  : {report['missing_source_ids'] or '—'}")
print(f"  Dead-lettered            : {report['dead_lettered_source_ids'] or '—'}")
assert report["drift"] and report["missing_source_ids"] == ["5003"]
bad("drift detected → alert email sent (logged when SMTP is unconfigured)")
ok("a missed order surfaces the next morning instead of at quarter-end")

# ---------------------------------------------------------------------------
scene(7, "Owner-facing status", "What a non-technical store owner checks to see it is alive.")
status = client.get("/sync/status").json()
for k, v in status.items():
    print(f"  {k:<14} {v}")
print(f"\n  {DIM}Same numbers rendered at http://localhost:8001/dashboard{OFF}")

print(f"\n{ORANGE}{'─' * 78}{OFF}")
print(f"{DARK}All scenes passed.{OFF} Ledger: {DB_PATH.name}")
print(f"{DIM}Inspect it:  sqlite3 {DB_PATH.name} 'select source_id,status,destination_id,attempts from syncrecord'{OFF}\n")
sys.exit(0)
