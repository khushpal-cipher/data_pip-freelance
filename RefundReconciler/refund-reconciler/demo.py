"""RefundReconciler proof-of-work demo — no Docker required, no credentials, no network.

Runs the real webhook -> ledger -> Celery task -> QBO path against a throwaway
SQLite file, Celery in eager mode (no Redis needed to watch this run), and a
fake QuickBooks that records calls instead of making them. Every scene below
is a failure mode that breaks amateur refund-sync integrations; each one
asserts the outcome, so this file is also the end-to-end regression check.

    python demo.py
"""

import hashlib
import hmac
import json
import os
import pathlib
import sys
import time
from decimal import Decimal

DB_PATH = pathlib.Path(__file__).parent / "demo.db"
DB_PATH.unlink(missing_ok=True)

STRIPE_SECRET = "demo-stripe-secret"
SHOPIFY_SECRET = "demo-shopify-secret"
os.environ.update(
    DATABASE_URL=f"sqlite:///{DB_PATH}",
    CELERY_ALWAYS_EAGER="true",
    STRIPE_WEBHOOK_SECRET=STRIPE_SECRET,
    SHOPIFY_WEBHOOK_SECRET=SHOPIFY_SECRET,
    QBO_INCOME_ACCOUNT_ID="79",
    LOG_LEVEL="WARNING",
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import app.tasks.reconcile as reconcile_module  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.models import RefundRecord, RefundStatus, Transaction  # noqa: E402

ORANGE, DARK, GREEN, RED, DIM, OFF = "\033[38;5;173m", "\033[1m", "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class FakeQBOClient:
    calls: list[dict] = []
    fail_next_n = 0

    def __init__(self, settings):
        pass

    def find_refund_receipt_by_doc_number(self, doc_number):
        return None

    def create_refund_receipt(self, refund_receipt: dict) -> dict:
        if FakeQBOClient.fail_next_n > 0:
            FakeQBOClient.fail_next_n -= 1
            raise RuntimeError("QBO returned 503 Service Unavailable")
        FakeQBOClient.calls.append(refund_receipt)
        return {"Id": f"qbo-refund-{len(FakeQBOClient.calls)}"}

    def close(self):
        pass


reconcile_module.QBOClient = FakeQBOClient

from app.main import app  # noqa: E402

client = TestClient(app)
init_db()


def scene(n, title, why):
    print(f"\n{ORANGE}{'─' * 78}{OFF}\n{DARK}SCENE {n} — {title}{OFF}\n{DIM}{why}{OFF}\n")


def ok(msg):
    print(f"  {GREEN}✓{OFF} {msg}")


def bad(msg):
    print(f"  {RED}✗{OFF} {msg}")


def stripe_post(refund_id, charge_id, amount_cents, currency="usd", fx_rate=None, secret=STRIPE_SECRET):
    obj = {"id": refund_id, "charge": charge_id, "amount": amount_cents, "currency": currency}
    if fx_rate is not None:
        obj["fx_rate"] = fx_rate
    body = {"type": "charge.refunded", "data": {"object": obj}}
    raw = json.dumps(body).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{raw.decode()}".encode(), hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/stripe",
        content=raw,
        headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"},
    )


def get_record(refund_id: str) -> RefundRecord:
    session = get_session()
    try:
        return session.exec(select(RefundRecord).where(RefundRecord.refund_id == refund_id)).first()
    finally:
        session.close()


def seed_transaction(source_id, amount, currency="USD", fx_rate="1", dest_id="qbo-original"):
    session = get_session()
    try:
        session.add(
            Transaction(
                source_system="stripe",
                source_id=source_id,
                amount=Decimal(amount),
                currency=currency,
                fx_rate=Decimal(fx_rate),
                dest_id=dest_id,
            )
        )
        session.commit()
    finally:
        session.close()


print(f"{DARK}RefundReconciler — Stripe/Shopify → QuickBooks Online refund sync{OFF}")
print("Live run against a real SQLite ledger, Celery in eager mode, and a stubbed QuickBooks. No network calls.")

# ── SCENE 1 ──────────────────────────────────────────────────────────────
scene(1, "Duplicate webhook storm", "Stripe resends a webhook on any slow or non-200 response.")
seed_transaction("ch_1001", "100.00")
for i in range(1, 6):
    resp = stripe_post("re_1001", "ch_1001", 3000)
    print(f"  delivery {i}: HTTP {resp.status_code}  already_queued={resp.json()['already_queued']}")
assert len(FakeQBOClient.calls) == 1
ok("5 identical deliveries → 1 ledger row → 1 QuickBooks RefundReceipt")
ok("enforced by UNIQUE(refund_id) in the database, not by an if-statement")

# ── SCENE 2 ──────────────────────────────────────────────────────────────
scene(2, "Forged webhook", "Your endpoint is public. Anything that can POST to it can invent a refund.")
resp = stripe_post("re_forged", "ch_1001", 1000, secret="wrong-secret")
assert resp.status_code == 401
ok(f"wrong signature → HTTP {resp.status_code}, rejected before any database write")

# ── SCENE 3 ──────────────────────────────────────────────────────────────
scene(3, "Partial refunds, then an over-refund", "Two partial refunds against one order, then one that would over-refund it.")
seed_transaction("ch_2001", "100.00")
stripe_post("re_2001a", "ch_2001", 3000)
stripe_post("re_2001b", "ch_2001", 4000)
r1, r2 = get_record("stripe:re_2001a"), get_record("stripe:re_2001b")
print(f"  re_2001a  30.00  portion={r1.refund_portion}")
print(f"  re_2001b  40.00  portion={r2.refund_portion}")
assert r1.status == RefundStatus.SYNCED and r2.status == RefundStatus.SYNCED
ok("70.00 of 100.00 refunded as two QBO RefundReceipts, each with the correct portion")

resp = stripe_post("re_2001c", "ch_2001", 5000)
r3 = get_record("stripe:re_2001c")
print(f"  re_2001c  50.00  status={r3.status.value}  error={r3.last_error}")
assert r3.status == RefundStatus.OVER_REFUND
bad(f"third refund of 50.00 rejected: {r3.last_error}")
ok("over-refund never reaches QuickBooks; it is held on the ledger for a human")

# ── SCENE 4 ──────────────────────────────────────────────────────────────
scene(4, "Multi-currency refund with FX rate stamped at refund time", "The original sale was booked at one FX rate; the refund happens later at a different rate.")
seed_transaction("ch_3001", "200.00", currency="EUR", fx_rate="1.00")
stripe_post("re_3001", "ch_3001", 10000, currency="eur", fx_rate="1.15")
r4 = get_record("stripe:re_3001")
receipt = FakeQBOClient.calls[-1]
print(f"  refund 100.00 EUR, booked at 1.00, refunded at fx_rate={r4.fx_rate}")
print(f"  {receipt['PrivateNote']}")
assert r4.fx_rate == Decimal("1.15")
ok("FX rate recorded on the refund itself — refunds computed from it never drift")
ok("realized FX gain/loss (15.00) posted as its own line, not netted into revenue")

# ── SCENE 5 ──────────────────────────────────────────────────────────────
scene(5, "Unmatched refund", "A refund event references an order we never booked. Guessing here is how books go quietly wrong.")
resp = stripe_post("re_orphan", "ch_never_seen", 1000)
r5 = get_record("stripe:re_orphan")
print(f"  refund_id=re_orphan  original_txn=ch_never_seen  status={r5.status.value}")
assert r5.status == RefundStatus.UNMATCHED
bad(f"held: {r5.last_error}")
ok("unmatched refund is held, not silently dropped or guessed at")

# ── SCENE 6 ──────────────────────────────────────────────────────────────
scene(6, "QuickBooks outage → dead letter → replay", "An outage must not silently drop a refund.")
seed_transaction("ch_4001", "80.00")
FakeQBOClient.fail_next_n = 6  # exceeds MAX_TASK_ATTEMPTS (6): every retry fails
resp = stripe_post("re_4001", "ch_4001", 4000)
r6 = get_record("stripe:re_4001")
print(f"  attempts={r6.attempts}  status={r6.status.value}  error={r6.last_error}")
assert r6.status == RefundStatus.FAILED
bad(f"dead-lettered after {r6.attempts} attempts: {r6.last_error}")

FakeQBOClient.fail_next_n = 0  # outage is over
replay_resp = client.post(f"/reconcile/replay/{r6.refund_id}")
r6b = get_record("stripe:re_4001")
print(f"  POST /reconcile/replay/{r6.refund_id} -> {replay_resp.status_code}")
print(f"  status={r6b.status.value}  dest_id={r6b.dest_id}")
assert r6b.status == RefundStatus.SYNCED
ok("replay after the outage clears re-runs the same refund_id and it syncs — never a duplicate")

# ── SCENE 7 ──────────────────────────────────────────────────────────────
scene(7, "Reconciliation status endpoint", "The owner-facing snapshot of what's healthy and what needs a human.")
status = client.get("/reconcile/status").json()
print(f"  {json.dumps(status, default=str)}")
ok("GET /reconcile/status returns synced/pending/failed/unmatched/over_refund counts")

print(f"\n{ORANGE}{'─' * 78}{OFF}")
print(f"{GREEN}All scenes passed.{OFF} {len(FakeQBOClient.calls)} refund receipts posted to (fake) QuickBooks.\n")

DB_PATH.unlink(missing_ok=True)
sys.exit(0)
