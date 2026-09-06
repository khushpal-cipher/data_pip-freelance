"""Both checks, run as one pass. The scheduler and the API both call this."""
from datetime import datetime
from typing import Optional

from sqlmodel import Session

from app.checks import freshness, volume
from app.models import utcnow


def run_all(session: Session, now: Optional[datetime] = None) -> list:
    now = now or utcnow()
    return freshness.run(session, now) + volume.run(session, now)
