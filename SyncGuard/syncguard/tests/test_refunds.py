import pytest
from sqlmodel import select

from app.config import get_settings
from app.models import RecordKind, SyncRecord, SyncStatus
from app.sync import ledger
from app.sync.refunds import RefundExceedsOriginalError, refund_to_qbo_refund_receipt
from app.worker import process_pending
from tests.conftest import sample_order, sample_refund


def _sync_order(session, order_id=8001, total="100.00"):
    order = sample_order(order_id=order_id, total=total)
    ledger.enqueue(session, source_id=str(order_id), kind=RecordKind.ORDER, payload=order)
    process_pending(session, settings=get_settings())
    return session.exec(select(SyncRecord).where(SyncRecord.source_id == str(order_id))).first()


def test_partial_refund_produces_refund_receipt_for_exact_amount(session, fake_qbo):
    order_record = _sync_order(session, order_id=8001, total="100.00")
    assert order_record.status == SyncStatus.SYNCED

    refund = sample_refund(refund_id=9001, order_id=8001, amount="30.00")
    ledger.enqueue(
        session,
        source_id="refund:9001",
        kind=RecordKind.REFUND,
        payload=refund,
        parent_source_id="8001",
    )
    result = process_pending(session, settings=get_settings())
    assert result["synced"] == 1

    refund_calls = [c for c in fake_qbo.calls if c["type"] == "refund_receipt"]
    assert len(refund_calls) == 1
    assert refund_calls[0]["receipt"]["TotalAmt"] == 30.00

    refund_record = session.exec(select(SyncRecord).where(SyncRecord.source_id == "refund:9001")).first()
    assert refund_record.status == SyncStatus.SYNCED


def test_full_refund_matches_order_total(session, fake_qbo):
    _sync_order(session, order_id=8002, total="75.50")

    refund = sample_refund(refund_id=9002, order_id=8002, amount="75.50")
    ledger.enqueue(
        session,
        source_id="refund:9002",
        kind=RecordKind.REFUND,
        payload=refund,
        parent_source_id="8002",
    )
    process_pending(session, settings=get_settings())

    refund_calls = [c for c in fake_qbo.calls if c["type"] == "refund_receipt"]
    assert refund_calls[0]["receipt"]["TotalAmt"] == 75.50


def test_refund_never_reaches_positive_sales_receipt_path(session, fake_qbo):
    _sync_order(session, order_id=8003, total="40.00")
    refund = sample_refund(refund_id=9003, order_id=8003, amount="40.00")
    ledger.enqueue(
        session,
        source_id="refund:9003",
        kind=RecordKind.REFUND,
        payload=refund,
        parent_source_id="8003",
    )
    process_pending(session, settings=get_settings())

    call_types = {c["type"] for c in fake_qbo.calls}
    assert "sales_receipt" in call_types  # from the original order sync
    refund_receipt_calls = [c for c in fake_qbo.calls if c["type"] == "refund_receipt"]
    assert len(refund_receipt_calls) == 1
    # the refund's own call must never be a sales_receipt
    assert all(c["type"] != "sales_receipt" for c in fake_qbo.calls[1:])


def test_refund_exceeding_order_total_is_rejected():
    settings = get_settings()
    refund = sample_refund(refund_id=9004, order_id=8004, amount="999.00")
    with pytest.raises(RefundExceedsOriginalError):
        refund_to_qbo_refund_receipt(
            refund,
            original_receipt_id="123",
            original_total=100.00,
            already_refunded=0.0,
            settings=settings,
        )


def test_second_partial_refund_accounts_for_already_refunded_amount(session, fake_qbo):
    _sync_order(session, order_id=8005, total="100.00")

    refund1 = sample_refund(refund_id=9005, order_id=8005, amount="40.00")
    ledger.enqueue(session, source_id="refund:9005", kind=RecordKind.REFUND, payload=refund1, parent_source_id="8005")
    process_pending(session, settings=get_settings())

    refund2 = sample_refund(refund_id=9006, order_id=8005, amount="60.00")
    ledger.enqueue(session, source_id="refund:9006", kind=RecordKind.REFUND, payload=refund2, parent_source_id="8005")
    process_pending(session, settings=get_settings())

    refund_calls = [c for c in fake_qbo.calls if c["type"] == "refund_receipt"]
    assert len(refund_calls) == 2
    assert refund_calls[1]["receipt"]["TotalAmt"] == 60.00

    # a third refund that would exceed the remaining balance dead-letters instead of overposting
    refund3 = sample_refund(refund_id=9007, order_id=8005, amount="10.00")
    ledger.enqueue(session, source_id="refund:9007", kind=RecordKind.REFUND, payload=refund3, parent_source_id="8005")
    result = process_pending(session, settings=get_settings())
    assert result["failed"] == 1
    refund_record = session.exec(select(SyncRecord).where(SyncRecord.source_id == "refund:9007")).first()
    assert "exceeds" in refund_record.last_error
