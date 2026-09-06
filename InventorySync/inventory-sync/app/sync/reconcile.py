import asyncio
import logging

import httpx

from app.config import CHANNELS

logger = logging.getLogger("inventorysync.reconcile")

MAX_RETRIES = 3


async def fan_out(sku_code: str, canonical_qty: int, exclude_channel: str) -> None:
    """Push the new canonical quantity to every channel except the one that
    just reported the change, so nobody sells against a stale number."""
    from app.main import app  # local import: avoids circular import with main.py

    targets = [c for c in CHANNELS if c != exclude_channel]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://internal") as client:
        for channel in targets:
            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.post(
                        f"/mock/{channel}/inventory",
                        json={"sku_code": sku_code, "qty": canonical_qty},
                    )
                    resp.raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    logger.warning(
                        "fan-out to %s failed (attempt %d): %s", channel, attempt + 1, exc
                    )
                    await asyncio.sleep(0.1 * (2**attempt))
            else:
                logger.error("fan-out to %s exhausted retries for %s", channel, sku_code)
