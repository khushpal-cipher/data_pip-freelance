"""Same webhook event_id delivered twice (retry, at-least-once delivery) must
only ever apply once."""

import json

from fastapi.testclient import TestClient

from app.main import app
from app.security import sign


def test_duplicate_event_id_applied_once(sku):
    payload = {"event_id": "evt-dup-1", "sku_code": sku.sku_code, "delta": -7}
    body = json.dumps(payload).encode()
    headers = {"x-signature": sign(body)}

    with TestClient(app) as client:
        r1 = client.post("/webhooks/shopify", content=body, headers=headers)
        r2 = client.post("/webhooks/shopify", content=body, headers=headers)

    assert r1.status_code == 200
    assert r1.json()["status"] == "applied"
    assert r1.json()["canonical_qty"] == sku.canonical_qty - 7

    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"

    r3 = TestClient(app).get(f"/stock/{sku.sku_code}")
    assert r3.json()["canonical_qty"] == sku.canonical_qty - 7


def test_bad_signature_rejected(sku):
    payload = {"event_id": "evt-bad-sig", "sku_code": sku.sku_code, "delta": -1}
    body = json.dumps(payload).encode()

    with TestClient(app) as client:
        r = client.post("/webhooks/shopify", content=body, headers={"x-signature": "wrong"})

    assert r.status_code == 401
