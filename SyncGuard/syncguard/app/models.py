from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SyncStatus(str, Enum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    SKIPPED = "skipped"


class RecordKind(str, Enum):
    ORDER = "order"
    REFUND = "refund"


class SyncRecord(SQLModel, table=True):
    """The idempotency ledger. One row per Shopify order or refund.

    UNIQUE(source_system, source_id) is what makes a webhook retry a no-op:
    the insert is done with ON CONFLICT DO NOTHING (see sync/ledger.py), so a
    duplicate delivery can never produce a second QBO receipt.
    """

    __table_args__ = (UniqueConstraint("source_system", "source_id", name="uq_source_system_source_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)

    source_system: str = Field(default="shopify", index=True)
    source_id: str = Field(index=True)
    kind: RecordKind = Field(default=RecordKind.ORDER)
    parent_source_id: Optional[str] = Field(default=None, index=True)

    source_updated_at: Optional[datetime] = None
    payload: str = Field(default="{}")  # raw Shopify JSON, so a replay never re-fetches

    destination_system: str = Field(default="qbo")
    destination_id: Optional[str] = None

    status: SyncStatus = Field(default=SyncStatus.PENDING, index=True)
    attempts: int = Field(default=0)
    last_error: Optional[str] = None
    next_attempt_at: datetime = Field(default_factory=utcnow, index=True)

    created_at: datetime = Field(default_factory=utcnow)
    synced_at: Optional[datetime] = None
