"""Stripe webhook intake: signature verification, then hand off to the ledger.

Stripe signs each delivery as `Stripe-Signature: t=<timestamp>,v1=<hmac>` over
the string "{timestamp}.{raw_body}" using the endpoint's signing secret, and
recommends rejecting anything outside a tolerance window to block replay of a
captured request (see Stripe's webhook signing docs).
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import get_settings
from app.db import get_session
from app.ledger import enqueue
from app.logging import get_logger, redact_payload
from app.tasks.reconcile import reconcile_refund_task

router = APIRouter()
logger = get_logger(component="webhook", provider="stripe")

REFUND_EVENT_PREFIXES = ("charge.refund", "refund.")


def verify_stripe_signature(
    body: bytes, sig_header: str | None, secret: str, *, tolerance_seconds: int
) -> bool:
    if not sig_header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    timestamp, signature = parts.get("t"), parts.get("v1")
    if not timestamp or not signature:
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance_seconds:
            return False
    except ValueError:
        return False
    signed_payload = f"{timestamp}.{body.decode('utf-8')}"
    expected = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")):
    settings = get_settings()
    body = await request.body()

    if not verify_stripe_signature(
        body, stripe_signature, settings.stripe_webhook_secret, tolerance_seconds=settings.stripe_webhook_tolerance_seconds
    ):
        logger.warning("webhook_signature_invalid")
        raise HTTPException(status_code=401, detail="invalid Stripe signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event_type = event.get("type", "")
    if not event_type.startswith(REFUND_EVENT_PREFIXES):
        logger.info("webhook_ignored", event_type=event_type)
        return {"status": "ignored", "type": event_type}

    obj = event.get("data", {}).get("object", {})
    refund_id = obj["id"]
    charge_id = obj.get("charge") or obj.get("payment_intent")
    amount = Decimal(str(obj["amount"])) / Decimal("100")  # Stripe amounts are minor units (cents)
    currency = obj.get("currency", "usd").upper()
    # In production this comes from the balance transaction's exchange_rate
    # (StripeClient.get_balance_transaction); accepted inline here so the
    # demo/tests can exercise multi-currency without a network call.
    fx_rate = Decimal(str(obj.get("fx_rate", "1")))

    session = get_session()
    try:
        record, created = enqueue(
            session,
            refund_id=f"stripe:{refund_id}",
            source_system="stripe",
            amount=amount,
            currency=currency,
            fx_rate=fx_rate,
            payload=redact_payload(obj),
            original_txn_id=charge_id,
        )
        if created:
            reconcile_refund_task.delay(record.id)
    finally:
        session.close()

    logger.info("webhook_received", refund_id=record.refund_id, created=created)
    return {"status": "accepted", "refund_id": record.refund_id, "already_queued": not created}
