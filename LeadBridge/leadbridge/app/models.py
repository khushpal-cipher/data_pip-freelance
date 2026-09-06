from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


class LeadStatus(str, Enum):
    received = "received"
    delivered = "delivered"
    failed = "failed"


class Lead(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str
    phone: str | None = None
    source: str = "website"
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: LeadStatus = Field(default=LeadStatus.received)
    attempts: int = Field(default=0)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
