"""The idempotency ledger.

Every Shopify order/refund event that could reach QBO is first inserted here
with an UPSERT/ON CONFLICT DO NOTHING against UNIQUE(source_system, source_id).
Whichever delivery wins the DB-level race gets processed; every retry (Shopify
resends webhooks liberally) finds its row already present and is a no-op. This
guarantee lives in the database, not in application logic, so it holds even
under concurrent workers or a crashed process retried by the webhook sender.
"""

import json
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models import RecordKind, SyncRecord, SyncStatus, utcnow


def enqueue(
    session: Session,
    *,
    source_id: str,
    kind: RecordKind,
    payload: dict,
    source_updated_at: Optional[datetime] = None,
    parent_source_id: Optional[str] = None,
    source_system: str = "shopify",
) -> tuple[SyncRecord, bool]:
    """Insert a ledger row if one doesn't already exist for this source_id.

    Returns (record, created). created=False means this exact event was
    already seen before (duplicate webhook delivery) — the caller does
    nothing further, which is what makes a retry a no-op.
    """
    existing = session.exec(
        select(SyncRecord).where(
            SyncRecord.source_system == source_system,
            SyncRecord.source_id == source_id,
        )
    ).first()
    if existing:
        return existing, False

    record = SyncRecord(
        source_system=source_system,
        source_id=source_id,
        kind=kind,
        parent_source_id=parent_source_id,
        source_updated_at=source_updated_at,
        payload=json.dumps(payload),
        status=SyncStatus.PENDING,
    )
    session.add(record)
    try:
        session.commit()
    except Exception:
        # Lost the race to a concurrent insert on the UNIQUE constraint:
        # someone else already created this row. Fetch and return it.
        session.rollback()
        existing = session.exec(
            select(SyncRecord).where(
                SyncRecord.source_system == source_system,
                SyncRecord.source_id == source_id,
            )
        ).first()
        if existing:
            return existing, False
        raise
    session.refresh(record)
    return record, True


def claim_due(session: Session, *, limit: int = 20) -> list[SyncRecord]:
    """Atomically claim a batch of pending/retry-due records for processing.

    Uses SELECT ... FOR UPDATE SKIP LOCKED where the backend supports it
    (Postgres) so concurrent workers never process the same row twice.
    SQLite (used in tests) ignores the clause but is single-threaded anyway.
    """
    now = utcnow()
    try:
        stmt = (
            select(SyncRecord)
            .where(SyncRecord.status == SyncStatus.PENDING)
            .where(SyncRecord.next_attempt_at <= now)
            .order_by(SyncRecord.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        records = list(session.exec(stmt))
    except Exception:
        session.rollback()
        stmt = (
            select(SyncRecord)
            .where(SyncRecord.status == SyncStatus.PENDING)
            .where(SyncRecord.next_attempt_at <= now)
            .order_by(SyncRecord.created_at)
            .limit(limit)
        )
        records = list(session.exec(stmt))
    return records


def mark_synced(session: Session, record: SyncRecord, *, destination_id: str) -> None:
    record.status = SyncStatus.SYNCED
    record.destination_id = destination_id
    record.synced_at = utcnow()
    record.last_error = None
    session.add(record)
    session.commit()


def mark_skipped(session: Session, record: SyncRecord, *, reason: str) -> None:
    record.status = SyncStatus.SKIPPED
    record.last_error = reason
    session.add(record)
    session.commit()


def mark_failed(session: Session, record: SyncRecord, *, error: str, max_attempts: int) -> None:
    record.attempts += 1
    record.last_error = error[:2000]
    if record.attempts >= max_attempts:
        record.status = SyncStatus.FAILED  # dead-lettered; replay via API to reset
    else:
        backoff_seconds = min(30 * (2 ** record.attempts), 6 * 3600)
        record.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds)
        record.status = SyncStatus.PENDING
    session.add(record)
    session.commit()


def replay(session: Session, source_id: str, *, source_system: str = "shopify") -> Optional[SyncRecord]:
    record = session.exec(
        select(SyncRecord).where(
            SyncRecord.source_system == source_system,
            SyncRecord.source_id == source_id,
        )
    ).first()
    if not record:
        return None
    record.status = SyncStatus.PENDING
    record.attempts = 0
    record.next_attempt_at = utcnow()
    record.last_error = None
    session.add(record)
    session.commit()
    return record


def get_status_counts(session: Session, *, since: datetime) -> dict:
    total_synced_today = len(
        list(
            session.exec(
                select(SyncRecord).where(
                    SyncRecord.status == SyncStatus.SYNCED,
                    SyncRecord.synced_at >= since,
                )
            )
        )
    )
    pending = len(list(session.exec(select(SyncRecord).where(SyncRecord.status == SyncStatus.PENDING))))
    failed = len(list(session.exec(select(SyncRecord).where(SyncRecord.status == SyncStatus.FAILED))))
    last_synced = session.exec(
        select(SyncRecord).where(SyncRecord.status == SyncStatus.SYNCED).order_by(SyncRecord.synced_at.desc())
    ).first()
    return {
        "last_sync": last_synced.synced_at if last_synced else None,
        "synced_today": total_synced_today,
        "failed": failed,
        "pending": pending,
    }
