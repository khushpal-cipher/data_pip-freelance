"""Raising, de-duplicating and resolving alerts — plus the outbound notification.

Every check routes through open_alert/resolve_alert, so de-duplication (never two open
alerts of the same type on the same source) is enforced in one place instead of in each
check.
"""
import logging
from typing import Optional

import httpx
from sqlmodel import Session, select

from app.config import ALERT_EMAIL_TO, SLACK_WEBHOOK_URL
from app.models import Alert, AlertType, Source, utcnow

log = logging.getLogger("datapipe.alerting")


def _open_alert(session: Session, source_id: int, type_: AlertType) -> Optional[Alert]:
    return session.exec(
        select(Alert).where(
            Alert.source_id == source_id,
            Alert.type == type_,
            Alert.resolved == False,  # noqa: E712 - SQL boolean, not Python identity
        )
    ).first()


def open_alert(session: Session, source: Source, type_: AlertType, message: str) -> Optional[Alert]:
    """Raise an alert unless one of the same type is already open. Returns the new alert,
    or None when it was suppressed as a duplicate."""
    if _open_alert(session, source.id, type_):
        return None

    alert = Alert(source_id=source.id, type=type_, message=message)
    session.add(alert)
    session.commit()
    session.refresh(alert)
    notify(source, alert)
    return alert


def resolve_alert(session: Session, source: Source, type_: AlertType) -> Optional[Alert]:
    """Close the open alert of this type, if there is one."""
    alert = _open_alert(session, source.id, type_)
    if alert is None:
        return None

    alert.resolved = True
    alert.resolved_at = utcnow()
    session.add(alert)
    session.commit()
    session.refresh(alert)
    log.info("resolved %s on %s", type_.value, source.name)
    return alert


def notify(source: Source, alert: Alert) -> None:
    """Slack when configured, log line otherwise. Never raises — a dead webhook must not
    take the scheduler down with it."""
    text = f":rotating_light: {source.name}: {alert.message}"
    log.warning("ALERT %s %s", source.name, alert.message)

    if ALERT_EMAIL_TO:
        log.info("would email %s: %s", ALERT_EMAIL_TO, text)

    if not SLACK_WEBHOOK_URL:
        return
    try:
        httpx.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=5)
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        log.error("slack notify failed: %s", exc)
