"""Claims pending ledger rows and syncs them to QBO.

This function is the only place that turns a ledger row into a QBO API call.
It is deliberately synchronous/sequential per record so a failure on one
record can never affect another, and it never calls QBO for a record whose
status isn't PENDING at claim time — claim_due() already filtered on that
via the ledger table, which is what keeps this idempotent under concurrent
invocations.
"""

import json

from sqlmodel import Session

from app.clients.qbo_client import QBOClient
from app.config import Settings, get_settings
from app.logging import get_logger
from app.models import RecordKind, SyncRecord, SyncStatus
from app.sync import ledger
from app.sync.order_to_receipt import order_to_qbo_receipt
from app.sync.refunds import refund_amount, refund_to_qbo_refund_receipt

logger = get_logger(component="worker")


def process_order(session: Session, record: SyncRecord, qbo: QBOClient, settings: Settings) -> str:
    order = json.loads(record.payload)
    receipt = order_to_qbo_receipt(order, settings=settings)
    created = qbo.create_sales_receipt(receipt)
    return str(created["Id"])


def process_refund(session: Session, record: SyncRecord, qbo: QBOClient, settings: Settings) -> str:
    refund = json.loads(record.payload)
    if not record.parent_source_id:
        raise ValueError(f"refund {record.source_id} has no parent_source_id (order not linked)")

    from sqlmodel import select

    original = session.exec(
        select(SyncRecord).where(
            SyncRecord.source_system == "shopify",
            SyncRecord.source_id == record.parent_source_id,
        )
    ).first()
    if not original or original.status != SyncStatus.SYNCED or not original.destination_id:
        raise ValueError(f"original order {record.parent_source_id} not yet synced; refund will retry")

    original_order = json.loads(original.payload)
    original_total = float(original_order.get("total_price", 0))

    already_refunded = 0.0
    for sibling in session.exec(
        select(SyncRecord).where(
            SyncRecord.parent_source_id == record.parent_source_id,
            SyncRecord.kind == RecordKind.REFUND,
            SyncRecord.status == SyncStatus.SYNCED,
        )
    ):
        already_refunded += refund_amount(json.loads(sibling.payload))

    refund_receipt = refund_to_qbo_refund_receipt(
        refund,
        original_receipt_id=original.destination_id,
        original_total=original_total,
        already_refunded=already_refunded,
        settings=settings,
    )
    created = qbo.create_refund_receipt(refund_receipt)
    return str(created["Id"])


def process_pending(session: Session, *, settings: Settings | None = None, batch_size: int = 20) -> dict:
    settings = settings or get_settings()
    records = ledger.claim_due(session, limit=batch_size)
    if not records:
        return {"processed": 0, "synced": 0, "failed": 0}

    qbo = QBOClient(settings)
    synced = failed = 0
    try:
        for record in records:
            log = logger.bind(source_id=record.source_id, kind=record.kind)
            try:
                if record.kind == RecordKind.ORDER:
                    dest_id = process_order(session, record, qbo, settings)
                else:
                    dest_id = process_refund(session, record, qbo, settings)
                ledger.mark_synced(session, record, destination_id=dest_id)
                synced += 1
                log.info("sync_succeeded", destination_id=dest_id)
            except Exception as exc:  # noqa: BLE001 - any failure must be captured on the ledger
                ledger.mark_failed(session, record, error=str(exc), max_attempts=settings.max_sync_attempts)
                failed += 1
                log.error("sync_failed", error=str(exc), attempts=record.attempts, status=record.status)
    finally:
        qbo.close()

    return {"processed": len(records), "synced": synced, "failed": failed}
