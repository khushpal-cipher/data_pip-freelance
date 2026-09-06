"""Sources, their heartbeats, and the alerts raised against them."""
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Naive UTC. Stored the same way on SQLite and Postgres, so comparisons never
    mix aware and naive datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Status(str, Enum):
    healthy = "healthy"
    stale = "stale"
    failing = "failing"


class AlertType(str, Enum):
    stale_data = "stale_data"
    volume_drop = "volume_drop"


class Source(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    expected_interval_minutes: int = 60
    max_staleness_minutes: int = 180
    last_record_at: Optional[datetime] = None
    status: Status = Status.healthy
    created_at: datetime = Field(default_factory=utcnow)

    def age_minutes(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.last_record_at is None:
            return None
        now = now or utcnow()
        return (now - self.last_record_at).total_seconds() / 60


class Heartbeat(SQLModel, table=True):
    """One delivery from a pipeline: it ran at `received_at` and wrote `row_count` rows."""
    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id", index=True)
    row_count: int = 0
    received_at: datetime = Field(default_factory=utcnow, index=True)


class Alert(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id", index=True)
    type: AlertType
    message: str
    created_at: datetime = Field(default_factory=utcnow, index=True)
    resolved: bool = Field(default=False, index=True)
    resolved_at: Optional[datetime] = None


def day_start(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def days_ago(n: int, now: Optional[datetime] = None) -> datetime:
    return (now or utcnow()) - timedelta(days=n)
