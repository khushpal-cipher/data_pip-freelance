from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RefundStatus(str, Enum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    UNMATCHED = "unmatched"       # original transaction could not be located
    OVER_REFUND = "over_refund"   # would refund more than the original total


class Transaction(SQLModel, table=True):
    """The original charge/order, as booked in the source system and in QBO.

    This is the "stored source_id mapping" refunds are located against: one
    row per Stripe charge / Shopify order, keyed by (source_system, source_id).
    fx_rate is the rate recorded at the time THIS transaction was booked —
    refunds compare against it but never overwrite it.

    Uniqueness is on (source_system, source_id), not source_id alone: a
    Stripe charge id and a Shopify order id live in separate namespaces and
    must be free to collide.
    """

    __table_args__ = (UniqueConstraint("source_system", "source_id", name="uq_txn_source_system_source_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)

    source_system: str = Field(index=True)  # "stripe" | "shopify"
    source_id: str = Field(index=True)

    amount: Decimal = Field(max_digits=14, decimal_places=4)
    currency: str = Field(max_length=3)
    fx_rate: Decimal = Field(default=Decimal("1"), max_digits=18, decimal_places=8)

    dest_id: Optional[str] = None  # QBO sales receipt / invoice id
    created_at: datetime = Field(default_factory=utcnow)


class RefundRecord(SQLModel, table=True):
    """The idempotency ledger. One row per refund event.

    UNIQUE(refund_id) is what makes a webhook retry a no-op: the insert is
    done with ON CONFLICT DO NOTHING (see ledger.py), so a duplicate delivery
    can never post a second refund to QuickBooks.
    """

    id: Optional[int] = Field(default=None, primary_key=True)

    refund_id: str = Field(index=True, unique=True)
    source_system: str = Field(default="stripe")
    original_txn_id: Optional[str] = Field(default=None, index=True)

    amount: Decimal = Field(max_digits=14, decimal_places=4)
    currency: str = Field(max_length=3)

    # FX rate recorded AT REFUND TIME. Never re-derived later, even if the
    # provider's rate for that date changes retroactively.
    fx_rate: Decimal = Field(default=Decimal("1"), max_digits=18, decimal_places=8)

    refund_portion: Optional[Decimal] = Field(default=None, max_digits=8, decimal_places=6)

    status: RefundStatus = Field(default=RefundStatus.PENDING, index=True)
    attempts: int = Field(default=0)
    last_error: Optional[str] = None
    next_attempt_at: datetime = Field(default_factory=utcnow, index=True)

    dest_id: Optional[str] = None  # QBO RefundReceipt id once synced
    payload: str = Field(default="{}")  # raw webhook JSON (redacted), for replay without re-fetch

    created_at: datetime = Field(default_factory=utcnow)
    processed_at: Optional[datetime] = None
