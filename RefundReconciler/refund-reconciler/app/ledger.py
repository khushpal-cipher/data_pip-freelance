"""The idempotency ledger.

Every refund webhook that could reach QuickBooks is first inserted here with
an insert against UNIQUE(refund_id). Whichever delivery wins the DB-level
race gets processed; every retry (Stripe and Shopify both resend webhooks
liberally on anything but a fast 200) finds its row already present and is a
no-op. This guarantee lives in the database, not in application logic, so it
holds even under concurrent Celery workers or a crashed task retried by the
broker. Same pattern as SyncGuard's SyncRecord ledger.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlmodel import Session, select

from app.models import RefundRecord, RefundStatus, utcnow


def enqueue(
    session: Session,
    *,
    refund_id: str,
    source_system: str,
    amount: Decimal,
    currency: str,
    payload: dict,
    fx_rate: Decimal = Decimal("1"),
    original_txn_id: Optional[str] = None,
) -> tuple[RefundRecord, bool]:
    """Insert a ledger row if one doesn't already exist for this refund_id.

    fx_rate is stamped here, once, at ingestion time — this is "the FX rate
    at refund time" the whole system promises never to re-derive later.

    Returns (record, created). created=False means this exact refund was
    already seen before (duplicate webhook delivery) — the caller does
    nothing further, which is what makes a retry a no-op.
    """
    existing = session.exec(select(RefundRecord).where(RefundRecord.refund_id == refund_id)).first()
    if existing:
        return existing, False

    record = RefundRecord(
        refund_id=refund_id,
        source_system=source_system,
        original_txn_id=original_txn_id,
        amount=amount,
        currency=currency,
        fx_rate=fx_rate,
        payload=json.dumps(payload),
        status=RefundStatus.PENDING,
    )
    session.add(record)
    try:
        session.commit()
    except Exception:
        # Lost the race to a concurrent insert on the UNIQUE constraint:
        # someone else already created this row. Fetch and return it.
        session.rollback()
        existing = session.exec(select(RefundRecord).where(RefundRecord.refund_id == refund_id)).first()
        if existing:
            return existing, False
        raise
    session.refresh(record)
    return record, True


def mark_synced(session: Session, record: RefundRecord, *, dest_id: str) -> None:
    record.status = RefundStatus.SYNCED
    record.dest_id = dest_id
    record.processed_at = utcnow()
    record.last_error = None
    session.add(record)
    session.commit()


def mark_unmatched(session: Session, record: RefundRecord, *, reason: str) -> None:
    record.status = RefundStatus.UNMATCHED
    record.last_error = reason
    session.add(record)
    session.commit()


def mark_over_refund(session: Session, record: RefundRecord, *, reason: str) -> None:
    record.status = RefundStatus.OVER_REFUND
    record.last_error = reason
    session.add(record)
    session.commit()


def mark_failed(session: Session, record: RefundRecord, *, error: str, max_attempts: int) -> None:
    record.attempts += 1
    record.last_error = error[:2000]
    if record.attempts >= max_attempts:
        record.status = RefundStatus.FAILED  # dead-lettered; replay via API to reset
    else:
        backoff_seconds = min(30 * (2 ** record.attempts), 6 * 3600)
        record.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds)
        record.status = RefundStatus.PENDING
    session.add(record)
    session.commit()


def replay(session: Session, refund_id: str) -> Optional[RefundRecord]:
    record = session.exec(select(RefundRecord).where(RefundRecord.refund_id == refund_id)).first()
    if not record:
        return None
    record.status = RefundStatus.PENDING
    record.attempts = 0
    record.next_attempt_at = utcnow()
    record.last_error = None
    session.add(record)
    session.commit()
    return record


def get_status_counts(session: Session, *, since: datetime) -> dict:
    synced_today = len(
        list(
            session.exec(
                select(RefundRecord).where(
                    RefundRecord.status == RefundStatus.SYNCED,
                    RefundRecord.processed_at >= since,
                )
            )
        )
    )
    pending = len(list(session.exec(select(RefundRecord).where(RefundRecord.status == RefundStatus.PENDING))))
    failed = len(list(session.exec(select(RefundRecord).where(RefundRecord.status == RefundStatus.FAILED))))
    unmatched = len(list(session.exec(select(RefundRecord).where(RefundRecord.status == RefundStatus.UNMATCHED))))
    over_refund = len(list(session.exec(select(RefundRecord).where(RefundRecord.status == RefundStatus.OVER_REFUND))))
    last_synced = session.exec(
        select(RefundRecord).where(RefundRecord.status == RefundStatus.SYNCED).order_by(RefundRecord.processed_at.desc())
    ).first()
    return {
        "last_sync": last_synced.processed_at if last_synced else None,
        "synced_today": synced_today,
        "pending": pending,
        "failed": failed,
        "unmatched": unmatched,
        "over_refund": over_refund,
    }
