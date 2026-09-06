"""Fake Shopify + marketplace receivers.

Standing in for real channel APIs so the fan-out step (reconcile.fan_out)
makes genuine HTTP calls end-to-end without needing real store credentials.
"""

from fastapi import APIRouter

mock_router = APIRouter()

# (channel, sku_code) -> last known qty on that channel
_mock_state: dict[tuple[str, str], int] = {}


@mock_router.post("/mock/{channel}/inventory")
async def mock_channel_update(channel: str, payload: dict):
    _mock_state[(channel, payload["sku_code"])] = payload["qty"]
    return {"ok": True}


@mock_router.get("/mock/{channel}/inventory/{sku_code}")
async def mock_channel_get(channel: str, sku_code: str):
    return {"qty": _mock_state.get((channel, sku_code))}
