import logging

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app.concurrency.locking import SkuNotFound, apply_delta
from app.db import engine
from app.models import ChannelEvent
from app.security import verify_signature
from app.sync.reconcile import fan_out

router = APIRouter()
logger = logging.getLogger("inventorysync.webhooks")


def _apply_event(channel: str, event_id: str, sku_code: str, delta: int) -> dict:
    """Blocking DB work, run off the event loop (see run_in_threadpool below) so
    concurrent requests genuinely race at the database instead of being
    serialized by a single asyncio event loop."""
    with Session(engine) as session:
        try:
            session.add(
                ChannelEvent(channel=channel, event_id=event_id, sku_code=sku_code, delta=delta)
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.info("duplicate event %s on %s, skipping", event_id, channel)
            return {"status": "duplicate", "event_id": event_id}

        try:
            sku = apply_delta(session, sku_code, delta)
        except SkuNotFound:
            return {"status": "not_found", "sku_code": sku_code}

        return {
            "status": "applied",
            "sku_code": sku_code,
            "canonical_qty": sku.canonical_qty,
            "version": sku.version,
        }


@router.post("/webhooks/{channel}")
async def inbound_webhook(
    channel: str, request: Request, x_signature: str | None = Header(default=None)
):
    body = await request.body()
    verify_signature(body, x_signature)
    payload = await request.json()

    result = await run_in_threadpool(
        _apply_event, channel, payload["event_id"], payload["sku_code"], payload["delta"]
    )

    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=f"unknown sku {result['sku_code']}")

    if result["status"] == "applied":
        await fan_out(result["sku_code"], result["canonical_qty"], exclude_channel=channel)

    return result
