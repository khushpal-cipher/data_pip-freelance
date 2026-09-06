"""SLA freshness: is the latest record for this source still inside its staleness budget?"""
from datetime import datetime
from typing import Optional, Tuple

from sqlmodel import Session, select

from app.alerting.notify import open_alert, resolve_alert
from app.models import AlertType, Source, Status, utcnow

# Past this multiple of the staleness budget the source isn't late, it's down.
FAILING_MULTIPLE = 2


def evaluate(source: Source, now: Optional[datetime] = None) -> Tuple[Status, Optional[str]]:
    """Pure: status the source should be in, and the alert message if it's breaching."""
    now = now or utcnow()
    budget = source.max_staleness_minutes

    if source.last_record_at is None:
        age = (now - source.created_at).total_seconds() / 60
        if age <= budget:
            return Status.healthy, None
        return Status.failing, (
            f"no data ever received — {age:.0f}m since registration, SLA is {budget}m"
        )

    age = source.age_minutes(now)
    if age <= budget:
        return Status.healthy, None

    status = Status.failing if age > budget * FAILING_MULTIPLE else Status.stale
    return status, (
        f"last record {age:.0f}m ago, SLA is {budget}m "
        f"(expected every {source.expected_interval_minutes}m)"
    )


def run(session: Session, now: Optional[datetime] = None) -> list:
    """Evaluate every source, update its status, open or resolve its freshness alert."""
    now = now or utcnow()
    raised = []

    for source in session.exec(select(Source)).all():
        status, message = evaluate(source, now)
        source.status = status
        session.add(source)
        session.commit()

        if message:
            alert = open_alert(session, source, AlertType.stale_data, message)
            if alert:
                raised.append(alert)
        else:
            resolve_alert(session, source, AlertType.stale_data)

    return raised
