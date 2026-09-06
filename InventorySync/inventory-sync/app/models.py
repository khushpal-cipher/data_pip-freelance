from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sku(SQLModel, table=True):
    """Canonical stock record. `version` is the optimistic-lock guard."""

    id: Optional[int] = Field(default=None, primary_key=True)
    sku_code: str = Field(unique=True, index=True)
    canonical_qty: int = 0
    version: int = 0


class ChannelEvent(SQLModel, table=True):
    """One inbound stock-change event. `event_id` unique constraint is the
    idempotency guard against double-applying retried webhooks."""

    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str
    event_id: str = Field(unique=True, index=True)
    sku_code: str
    delta: int
    applied_at: datetime = Field(default_factory=_utcnow)
