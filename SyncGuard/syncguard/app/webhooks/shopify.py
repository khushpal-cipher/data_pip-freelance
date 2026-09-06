import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.logging import get_logger
from app.models import RecordKind
from app.sync import ledger

router = APIRouter()
logger = get_logger(component="webhook")


def _parse_shopify_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def verify_hmac(body: bytes, hmac_header: str | None, secret: str) -> bool:
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

    if not verify_hmac(body, x_shopify_hmac_sha256, settings.shopify_webhook_secret):
        logger.warning("webhook_hmac_invalid", topic=x_shopify_topic)
        raise HTTPException(status_code=401, detail="invalid HMAC signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    topic = x_shopify_topic or ""
    session: Session = get_session()
    try:
        if topic.startswith("refunds/") or "refund_line_items" in payload:
            source_id = f"refund:{payload['id']}"
            order_id = payload.get("order_id")
            record, created = ledger.enqueue(
                session,
                source_id=source_id,
                kind=RecordKind.REFUND,
                payload=payload,
                parent_source_id=str(order_id) if order_id else None,
            )
        else:
            source_id = str(payload["id"])
            record, created = ledger.enqueue(
                session,
                source_id=source_id,
                kind=RecordKind.ORDER,
                payload=payload,
                source_updated_at=_parse_shopify_timestamp(payload.get("updated_at")),
            )
    finally:
        session.close()

    logger.info("webhook_received", source_id=source_id, topic=topic, created=created)
    return {"status": "accepted", "source_id": source_id, "already_queued": not created}
