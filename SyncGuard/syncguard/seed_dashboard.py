"""Populate syncguard.db with realistic rows so /dashboard has something to show.

Uses the real webhook -> ledger -> worker path against a stubbed QuickBooks.
Run once, then reload http://127.0.0.1:8001

    python seed_dashboard.py
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./syncguard.db")
os.environ.update(
    SHOPIFY_WEBHOOK_SECRET="seed-secret",
    SHOPIFY_STORE_DOMAIN="demo-store.myshopify.com",
    QBO_INCOME_ACCOUNT_ID="79",
    QBO_FEE_ACCOUNT_ID="80",
    QBO_CLEARING_ACCOUNT_ID="81",
    LOG_LEVEL="WARNING",
)

import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import worker  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.main import app  # noqa: E402


class FakeQBO:
    down = False
    _next_id = 1040

    def __init__(self, settings=None):
        pass

    def _create(self, payload):
        if FakeQBO.down:
            raise RuntimeError("QBO returned 503 Service Unavailable")
        FakeQBO._next_id += 1
        return {"Id": str(FakeQBO._next_id), "DocNumber": payload["DocNumber"]}

    create_sales_receipt = _create
    create_refund_receipt = _create

    def close(self):
        pass


worker.QBOClient = FakeQBO
settings = get_settings()
init_db()
client = TestClient(app)


def post(payload, topic):
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(b"seed-secret", body, hashlib.sha256).digest()).decode()
    return client.post(
        "/webhooks/shopify",
        content=body,
        headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": topic},
    )


def order(oid, total, item, qty, price, fee):
    return {
        "id": oid,
        "created_at": "2026-09-04T09:00:00-05:00",
        "updated_at": "2026-09-04T09:00:00-05:00",
        "currency": "USD",
        "total_price": f"{total:.2f}",
        "email": f"buyer{oid}@example.com",
        "line_items": [{"title": item, "quantity": qty, "price": f"{price:.2f}"}],
        "shopify_payout_fee": fee,
    }


def run_worker():
    s = get_session()
    try:
        return worker.process_pending(s, settings=settings)
    finally:
        s.close()


ORDERS = [
    (6001, 149.00, "Ceramic Planter", 2, 74.50, 4.62),
    (6002, 89.00, "Linen Apron", 1, 89.00, 2.88),
    (6003, 240.00, "Cast Iron Skillet", 1, 240.00, 7.26),
    (6004, 62.50, "Beeswax Candle Set", 5, 12.50, 2.11),
]

for o in ORDERS:
    post(order(*o), "orders/create")
run_worker()

# one order caught mid-outage: retrying, visible under "Waiting"
FakeQBO.down = True
post(order(6005, 310.00, "Walnut Cutting Board", 1, 310.00, 9.29), "orders/create")
run_worker()
FakeQBO.down = False

# one refund against an already-synced order
post(
    {
        "id": 7101,
        "order_id": 6001,
        "created_at": "2026-09-04T11:00:00-05:00",
        "currency": "USD",
        "transactions": [{"amount": "74.50"}],
        "refund_line_items": [],
    },
    "refunds/create",
)
run_worker()

print("Seeded syncguard.db — reload http://127.0.0.1:8001")
