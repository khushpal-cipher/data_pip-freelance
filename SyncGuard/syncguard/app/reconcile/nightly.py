"""Nightly reconciliation: the retainer justification.

Counts Shopify orders for the prior UTC day against QBO sales receipts
created from this system for the same day, using the ledger as the source of
truth for "should have synced". Any Shopify order without a corresponding
SYNCED ledger row is drift — silent data loss that duplicate-checks and
refund logic alone can't catch (an order the webhook never reached, a QBO
outage that dead-lettered a record, etc). This is what a client is paying an
ongoing retainer for: someone/something checking every single night that the
two systems still agree, not just that the code runs.
"""

import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from sqlmodel import Session, select

from app.clients.shopify_client import ShopifyClient
from app.config import Settings, get_settings
from app.logging import get_logger
from app.models import RecordKind, SyncRecord, SyncStatus

logger = get_logger(component="reconcile")


def _day_bounds(reference: datetime | None = None) -> tuple[datetime, datetime]:
    now = reference or datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).date()
    start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def run_reconciliation(session: Session, *, settings: Settings | None = None, reference: datetime | None = None) -> dict:
    settings = settings or get_settings()
    start, end = _day_bounds(reference)

    shopify_order_ids: set[str] = set()
    try:
        shopify = ShopifyClient(settings)
        shopify_order_ids = set(
            shopify.list_order_ids(created_at_min=start.isoformat(), created_at_max=end.isoformat())
        )
        shopify.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("reconcile_shopify_fetch_failed", error=str(exc))

    synced_rows = session.exec(
        select(SyncRecord).where(
            SyncRecord.kind == RecordKind.ORDER,
            SyncRecord.status == SyncStatus.SYNCED,
            SyncRecord.synced_at >= start,
            SyncRecord.synced_at < end,
        )
    ).all()
    synced_ids = {r.source_id for r in synced_rows}

    missing = sorted(shopify_order_ids - synced_ids)

    failed_rows = session.exec(
        select(SyncRecord).where(
            SyncRecord.kind == RecordKind.ORDER,
            SyncRecord.status == SyncStatus.FAILED,
        )
    ).all()

    report = {
        "date": start.date().isoformat(),
        "shopify_order_count": len(shopify_order_ids),
        "qbo_synced_count": len(synced_ids),
        "missing_source_ids": missing,
        "dead_lettered_source_ids": [r.source_id for r in failed_rows],
        "drift": bool(missing) or bool(failed_rows),
    }

    if report["drift"]:
        logger.warning("reconciliation_drift_detected", **report)
        _send_alert(report, settings=settings)
    else:
        logger.info("reconciliation_clean", **report)

    return report


def _send_alert(report: dict, *, settings: Settings) -> None:
    body_lines = [
        f"SyncGuard reconciliation drift for {report['date']}",
        f"Shopify orders: {report['shopify_order_count']}",
        f"QBO synced:     {report['qbo_synced_count']}",
        "",
        f"Missing source_ids ({len(report['missing_source_ids'])}):",
        *[f"  - {sid}" for sid in report["missing_source_ids"]],
        "",
        f"Dead-lettered source_ids ({len(report['dead_lettered_source_ids'])}):",
        *[f"  - {sid}" for sid in report["dead_lettered_source_ids"]],
    ]
    body = "\n".join(body_lines)

    if not settings.smtp_host or not settings.alert_email_to:
        logger.warning("reconciliation_alert_email_not_configured_logging_instead", body=body)
        return

    msg = MIMEText(body)
    msg["Subject"] = f"[SyncGuard] Reconciliation drift detected — {report['date']}"
    msg["From"] = settings.alert_email_from or settings.smtp_user
    msg["To"] = settings.alert_email_to

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(msg["From"], [settings.alert_email_to], msg.as_string())
        logger.info("reconciliation_alert_email_sent", to=settings.alert_email_to)
    except Exception as exc:  # noqa: BLE001
        logger.error("reconciliation_alert_email_failed", error=str(exc))
