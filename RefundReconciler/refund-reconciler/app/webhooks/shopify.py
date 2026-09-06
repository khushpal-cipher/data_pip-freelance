"""Shopify webhook intake: HMAC verification, then hand off to the ledger.

Shopify signs each delivery as `X-Shopify-Hmac-Sha256`: base64(HMAC-SHA256(raw
body, shared secret)). Same verify-then-enqueue shape as SyncGuard's Shopify
webhook, scoped here to refund events only.
"""

import base64
import hashlib
import hmac
import json
from decimal import Decimal

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import get_settings
from app.db import get_session
from app.ledger import enqueue
from app.logging import get_logger, redact_payload
from app.tasks.reconcile import reconcile_refund_task

router = APIRouter()
logger = get_logger(component="webhook", provider="shopify")


def verify_shopify_hmac(body: bytes, hmac_header: str | None, secret: str) -> bool:
    if not hmac_header or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header)


@router.post("/webhooks/shopify")
async def shopify_webhook(
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_topic: str | None = Header(default=None),
):
    settings = get_settings()
    body = await request.body()

    if not verify_shopify_hmac(body, x_shopify_hmac_sha256, settings.shopify_webhook_secret):
        logger.warning("webhook_signature_invalid", topic=x_shopify_topic)
        raise HTTPException(status_code=401, detail="invalid Shopify HMAC signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    topic = x_shopify_topic or ""
    if not (topic.startswith("refunds/") or "refund_line_items" in payload or "transactions" in payload):
        logger.info("webhook_ignored", topic=topic)
        return {"status": "ignored", "topic": topic}

    refund_id = payload["id"]
    order_id = payload.get("order_id")
    # Shopify refunds carry the refunded amount on the nested transactions array.
    transactions = payload.get("transactions") or []
    amount = Decimal(str(sum(Decimal(str(t.get("amount", "0"))) for t in transactions))) if transactions else Decimal("0")
    currency = (transactions[0].get("currency") if transactions else payload.get("currency", "USD")) or "USD"
    fx_rate = Decimal(str(payload.get("fx_rate", "1")))

    session = get_session()
    try:
        record, created = enqueue(
            session,
            refund_id=f"shopify:{refund_id}",
            source_system="shopify",
            amount=amount,
            currency=currency.upper(),
            fx_rate=fx_rate,
            payload=redact_payload(payload),
            original_txn_id=str(order_id) if order_id else None,
        )
        if created:
            reconcile_refund_task.delay(record.id)
    finally:
        session.close()

    logger.info("webhook_received", refund_id=record.refund_id, created=created)
    return {"status": "accepted", "refund_id": record.refund_id, "already_queued": not created}
