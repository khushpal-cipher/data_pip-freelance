"""Volume anomaly: today's row count against the source's own 7-day rolling average.

A pipeline that keeps its heartbeat but suddenly writes a tenth of the usual rows is the
silent failure freshness alone can't see.
"""
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlmodel import Session, select

from app.alerting.notify import open_alert, resolve_alert
from app.config import VOLUME_DROP_THRESHOLD, VOLUME_MIN_BASELINE
from app.models import AlertType, Heartbeat, Source, utcnow

BASELINE_DAYS = 7


def _rows_between(session: Session, source_id: int, start: datetime, end: datetime) -> int:
    rows = session.exec(
        select(Heartbeat).where(
            Heartbeat.source_id == source_id,
            Heartbeat.received_at >= start,
            Heartbeat.received_at < end,
        )
    ).all()
    return sum(h.row_count for h in rows)


def evaluate(
    session: Session, source: Source, now: Optional[datetime] = None
) -> Tuple[int, float, float, Optional[str]]:
    """Returns (today_rows, baseline_avg, drop_fraction, alert message or None).

    "Today" is the trailing 24h and the baseline is the 7 days before it, so the result
    doesn't swing with the time of day the check happens to run.
    """
    now = now or utcnow()
    today_start = now - timedelta(days=1)
    baseline_start = now - timedelta(days=1 + BASELINE_DAYS)

    today = _rows_between(session, source.id, today_start, now)
    baseline_total = _rows_between(session, source.id, baseline_start, today_start)
    baseline = baseline_total / BASELINE_DAYS

    if baseline < VOLUME_MIN_BASELINE:
        return today, baseline, 0.0, None  # too little history to call anything a drop

    drop = (baseline - today) / baseline
    if drop < VOLUME_DROP_THRESHOLD:
        return today, baseline, max(drop, 0.0), None

    return today, baseline, drop, (
        f"volume down {drop * 100:.0f}% — {today} rows in 24h vs "
        f"{baseline:.0f}/day over the last {BASELINE_DAYS} days"
    )


def run(session: Session, now: Optional[datetime] = None) -> list:
    now = now or utcnow()
    raised = []

    for source in session.exec(select(Source)).all():
        _, _, _, message = evaluate(session, source, now)
        if message:
            alert = open_alert(session, source, AlertType.volume_drop, message)
            if alert:
                raised.append(alert)
        else:
            resolve_alert(session, source, AlertType.volume_drop)

    return raised
