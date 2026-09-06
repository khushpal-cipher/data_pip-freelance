import hashlib
import hmac
import json
import time

from app.webhooks.shopify import verify_shopify_hmac
from app.webhooks.stripe import verify_stripe_signature


def _stripe_sig(body: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.{body.decode('utf-8')}"
    sig = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_stripe_signature_accepts_valid():
    body = json.dumps({"id": "evt_1"}).encode()
    header = _stripe_sig(body, "whsec_test")
    assert verify_stripe_signature(body, header, "whsec_test", tolerance_seconds=300) is True


def test_stripe_signature_rejects_tampered_body():
    body = json.dumps({"id": "evt_1"}).encode()
    header = _stripe_sig(body, "whsec_test")
    tampered = json.dumps({"id": "evt_EVIL"}).encode()
    assert verify_stripe_signature(tampered, header, "whsec_test", tolerance_seconds=300) is False


def test_stripe_signature_rejects_wrong_secret():
    body = json.dumps({"id": "evt_1"}).encode()
    header = _stripe_sig(body, "whsec_test")
    assert verify_stripe_signature(body, header, "whsec_WRONG", tolerance_seconds=300) is False


def test_stripe_signature_rejects_stale_timestamp():
    body = json.dumps({"id": "evt_1"}).encode()
    old_ts = int(time.time()) - 10_000
    header = _stripe_sig(body, "whsec_test", timestamp=old_ts)
    assert verify_stripe_signature(body, header, "whsec_test", tolerance_seconds=300) is False


def test_stripe_signature_rejects_missing_header():
    body = b"{}"
    assert verify_stripe_signature(body, None, "whsec_test", tolerance_seconds=300) is False


def test_shopify_hmac_accepts_valid():
    body = json.dumps({"id": 123}).encode()
    secret = "shopify-secret"
    import base64

    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    header = base64.b64encode(digest).decode()
    assert verify_shopify_hmac(body, header, secret) is True


def test_shopify_hmac_rejects_tampered_body():
    secret = "shopify-secret"
    body = json.dumps({"id": 123}).encode()
    import base64

    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    header = base64.b64encode(digest).decode()
    tampered = json.dumps({"id": 999}).encode()
    assert verify_shopify_hmac(tampered, header, secret) is False
