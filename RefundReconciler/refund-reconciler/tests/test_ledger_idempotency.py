from decimal import Decimal

from app import ledger
from app.models import RefundStatus


def test_enqueue_creates_new_record(session):
    record, created = ledger.enqueue(
        session,
        refund_id="stripe:re_1",
        source_system="stripe",
        amount=Decimal("30.00"),
        currency="USD",
        payload={"id": "re_1"},
        original_txn_id="ch_1",
    )
    assert created is True
    assert record.status == RefundStatus.PENDING


def test_duplicate_refund_id_is_a_noop(session):
    first, created1 = ledger.enqueue(
        session,
        refund_id="stripe:re_dup",
        source_system="stripe",
        amount=Decimal("30.00"),
        currency="USD",
        payload={"id": "re_dup"},
        original_txn_id="ch_1",
    )
    assert created1 is True

    second, created2 = ledger.enqueue(
        session,
        refund_id="stripe:re_dup",
        source_system="stripe",
        amount=Decimal("30.00"),
        currency="USD",
        payload={"id": "re_dup"},
        original_txn_id="ch_1",
    )
    assert created2 is False
    assert second.id == first.id


def test_five_duplicate_deliveries_yield_one_row(session):
    ids = set()
    for _ in range(5):
        record, _ = ledger.enqueue(
            session,
            refund_id="stripe:re_storm",
            source_system="stripe",
            amount=Decimal("10.00"),
            currency="USD",
            payload={},
            original_txn_id="ch_2",
        )
        ids.add(record.id)
    assert len(ids) == 1


def test_mark_synced_then_failed_then_replay(session):
    record, _ = ledger.enqueue(
        session,
        refund_id="stripe:re_lifecycle",
        source_system="stripe",
        amount=Decimal("15.00"),
        currency="USD",
        payload={},
        original_txn_id="ch_3",
    )
    ledger.mark_failed(session, record, error="QBO 503", max_attempts=2)
    assert record.status == RefundStatus.PENDING
    assert record.attempts == 1

    ledger.mark_failed(session, record, error="QBO 503", max_attempts=2)
    assert record.status == RefundStatus.FAILED  # dead-lettered after max_attempts

    replayed = ledger.replay(session, "stripe:re_lifecycle")
    assert replayed.status == RefundStatus.PENDING
    assert replayed.attempts == 0
