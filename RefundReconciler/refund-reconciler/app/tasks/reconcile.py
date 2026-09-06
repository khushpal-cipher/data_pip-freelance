"""The refund reconciliation task: one Celery task per refund event.

Flow: locate the original transaction -> compute the refund portion and
over-refund guard -> stamp the FX rate recorded at refund time -> post a
RefundReceipt to QBO -> mark synced. Any exception short of an explicit
UNMATCHED/OVER_REFUND classification is retried with exponential backoff up
to settings.max_task_attempts, after which the ledger row is left FAILED
(dead-lettered) for a human to inspect and POST /reconcile/replay/{id}.
"""

from decimal import Decimal

from sqlmodel import Session, select

from app import ledger
from app.celery_app import celery_app
from app.clients.qbo_client import QBOClient
from app.config import Settings, get_settings
from app.currency.convert import (
    OverRefundError,
    assert_not_over_refunded,
    calculate_refund_portion,
    fx_gain_loss,
    to_base_currency,
)
from app.db import get_session
from app.logging import get_logger
from app.matching.locate import find_original
from app.models import RefundRecord, RefundStatus

logger = get_logger(component="reconcile")


class UnmatchedTransactionError(Exception):
    pass


def _already_refunded(session: Session, *, original_txn_id: str, exclude_id: int) -> Decimal:
    rows = session.exec(
        select(RefundRecord).where(
            RefundRecord.original_txn_id == original_txn_id,
            RefundRecord.status == RefundStatus.SYNCED,
            RefundRecord.id != exclude_id,
        )
    ).all()
    return sum((r.amount for r in rows), Decimal("0"))


def process_refund(session: Session, record: RefundRecord, *, settings: Settings, qbo: QBOClient) -> dict:
    """Pure-ish orchestration: raises UnmatchedTransactionError / OverRefundError for
    classification by the caller, or any other exception to signal a retryable failure."""
    original = find_original(session, source_system=record.source_system, original_txn_id=record.original_txn_id or "")
    if original is None:
        raise UnmatchedTransactionError(
            f"no original transaction found for {record.source_system}:{record.original_txn_id}"
        )

    already_refunded = _already_refunded(session, original_txn_id=original.source_id, exclude_id=record.id)
    assert_not_over_refunded(
        refund_amount=record.amount, already_refunded=already_refunded, original_amount=original.amount
    )
    portion = calculate_refund_portion(record.amount, original.amount)

    base_amount = to_base_currency(record.amount, record.fx_rate)
    gain_loss = fx_gain_loss(
        refund_amount=record.amount, refund_fx_rate=record.fx_rate, booking_fx_rate=original.fx_rate
    )

    receipt = qbo.create_refund_receipt(
        {
            "DocNumber": f"REFUND-{record.refund_id}",
            "TotalAmt": str(record.amount),
            "CurrencyRef": {"value": record.currency},
            "ExchangeRate": str(record.fx_rate),
            "PrivateNote": (
                f"portion={portion:.6f} base_amount={base_amount} "
                f"fx_gain_loss={gain_loss} original_txn={original.source_id}"
            ),
            "LinkedTxn": [{"TxnId": original.dest_id, "TxnType": "SalesReceipt"}] if original.dest_id else [],
        }
    )

    record.refund_portion = portion
    session.add(record)

    logger.info(
        "refund_processed",
        refund_id=record.refund_id,
        original_txn_id=original.source_id,
        portion=str(portion),
        base_amount=str(base_amount),
        fx_gain_loss=str(gain_loss),
    )
    return {"dest_id": receipt["Id"], "portion": portion, "base_amount": base_amount, "fx_gain_loss": gain_loss}


@celery_app.task(bind=True, name="reconcile_refund", max_retries=None)
def reconcile_refund_task(self, refund_record_id: int) -> dict:
    settings = get_settings()
    session = get_session()
    qbo = QBOClient(settings)
    try:
        record = session.get(RefundRecord, refund_record_id)
        if record is None:
            logger.warning("reconcile_refund_missing_record", refund_record_id=refund_record_id)
            return {"skipped": True}
        if record.status != RefundStatus.PENDING:
            return {"skipped": True, "status": record.status}

        try:
            result = process_refund(session, record, settings=settings, qbo=qbo)
        except UnmatchedTransactionError as exc:
            ledger.mark_unmatched(session, record, reason=str(exc))
            logger.warning("refund_unmatched", refund_id=record.refund_id, error=str(exc))
            return {"status": "unmatched"}
        except OverRefundError as exc:
            ledger.mark_over_refund(session, record, reason=str(exc))
            logger.error("refund_over_refund", refund_id=record.refund_id, error=str(exc))
            return {"status": "over_refund"}
        except Exception as exc:  # noqa: BLE001 - classify as retryable failure
            ledger.mark_failed(session, record, error=str(exc), max_attempts=settings.max_task_attempts)
            logger.error(
                "refund_sync_failed", refund_id=record.refund_id, attempts=record.attempts, error=str(exc)
            )
            if record.status == RefundStatus.FAILED:
                return {"status": "dead_lettered", "attempts": record.attempts}
            backoff = min(
                settings.retry_backoff_base_seconds * (2 ** record.attempts),
                settings.retry_backoff_max_seconds,
            )
            raise self.retry(exc=exc, countdown=backoff)

        ledger.mark_synced(session, record, dest_id=result["dest_id"])
        return {"status": "synced", "dest_id": result["dest_id"]}
    finally:
        qbo.close()
        session.close()
