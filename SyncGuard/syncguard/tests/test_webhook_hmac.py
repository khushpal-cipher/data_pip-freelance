import json

from tests.conftest import sample_order, sign


def _body(order_id=1111):
    return json.dumps(sample_order(order_id=order_id)).encode("utf-8")


def test_valid_signature_is_accepted(client):
    body = _body(1111)
    resp = client.post(
        "/webhooks/shopify",
        content=body,
        headers={"X-Shopify-Hmac-Sha256": sign(body), "X-Shopify-Topic": "orders/create"},
    )
    assert resp.status_code == 200
    assert resp.json()["source_id"] == "1111"


def test_missing_signature_header_is_rejected(client):
    body = _body(1112)
    resp = client.post("/webhooks/shopify", content=body, headers={"X-Shopify-Topic": "orders/create"})
    assert resp.status_code == 401


def test_signature_from_wrong_secret_is_rejected(client):
    body = _body(1113)
    wrong_sig = sign(body, secret="wrong-secret")
    resp = client.post(
        "/webhooks/shopify",
        content=body,
        headers={"X-Shopify-Hmac-Sha256": wrong_sig, "X-Shopify-Topic": "orders/create"},
    )
    assert resp.status_code == 401


def test_tampered_body_after_signing_is_rejected(client):
    body = _body(1114)
    sig = sign(body)  # sign the original body
    tampered = body.replace(b"1114", b"9999")  # then mutate it, as a MITM would
    resp = client.post(
        "/webhooks/shopify",
        content=tampered,
        headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create"},
    )
    assert resp.status_code == 401


def test_health_check_does_not_require_signature(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
