import json

from sqlmodel import Session, select

from app.config import get_settings
from app.models import RecordKind, SyncRecord, SyncStatus
from app.sync import ledger
from app.worker import process_pending
from tests.conftest import sample_order, sign


def _json_bytes(obj: dict) -> bytes:
    return json.dumps(obj).encode("utf-8")


def test_duplicate_webhook_creates_one_ledger_row_and_one_qbo_call(client, engine, fake_qbo):
    order = sample_order(order_id=7001)
    body = _json_bytes(order)
    headers = {"X-Shopify-Hmac-Sha256": sign(body), "X-Shopify-Topic": "orders/create"}

    r1 = client.post("/webhooks/shopify", content=body, headers=headers)
    r2 = client.post("/webhooks/shopify", content=body, headers=headers)  # exact retry

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["already_queued"] is False
    assert r2.json()["already_queued"] is True

    with Session(engine) as session:
        rows = session.exec(select(SyncRecord).where(SyncRecord.source_id == "7001")).all()
        assert len(rows) == 1

        result = process_pending(session, settings=get_settings())
        assert result["synced"] == 1

        result2 = process_pending(session, settings=get_settings())
        assert result2["processed"] == 0

    assert len(fake_qbo.calls) == 1
    assert fake_qbo.calls[0]["type"] == "sales_receipt"


def test_ledger_insert_is_atomic_under_direct_double_enqueue(session):
    order = sample_order(order_id=7002)
    rec1, created1 = ledger.enqueue(session, source_id="7002", kind=RecordKind.ORDER, payload=order)
    rec2, created2 = ledger.enqueue(session, source_id="7002", kind=RecordKind.ORDER, payload=order)

    assert created1 is True
    assert created2 is False
    assert rec1.id == rec2.id

    rows = session.exec(select(SyncRecord).where(SyncRecord.source_id == "7002")).all()
    assert len(rows) == 1


def test_invalid_hmac_is_rejected_and_no_ledger_row_created(client, engine):
    order = sample_order(order_id=7003)
    body = _json_bytes(order)

    resp = client.post(
        "/webhooks/shopify",
        content=body,
        headers={"X-Shopify-Hmac-Sha256": "not-a-valid-signature", "X-Shopify-Topic": "orders/create"},
    )
    assert resp.status_code == 401

    with Session(engine) as session:
        rows = session.exec(select(SyncRecord).where(SyncRecord.source_id == "7003")).all()
        assert len(rows) == 0


def test_dead_letter_after_max_attempts_and_replay_resets_it(session, monkeypatch):
    order = sample_order(order_id=7004)
    ledger.enqueue(session, source_id="7004", kind=RecordKind.ORDER, payload=order)

    def boom(*args, **kwargs):
        raise RuntimeError("QBO sandbox unreachable")

    monkeypatch.setattr("app.worker.process_order", boom)

    settings = get_settings()
    monkeypatch.setattr(settings, "max_sync_attempts", 3)

    for _ in range(3):
        result = process_pending(session, settings=settings)
        assert result["processed"] == 1
        record = session.exec(select(SyncRecord).where(SyncRecord.source_id == "7004")).first()
        if record.status != SyncStatus.FAILED:
            record.next_attempt_at = record.next_attempt_at.replace(year=2000)  # force due immediately
            session.add(record)
            session.commit()

    refreshed = session.exec(select(SyncRecord).where(SyncRecord.source_id == "7004")).first()
    assert refreshed.status == SyncStatus.FAILED
    assert refreshed.attempts == 3

    ledger.replay(session, "7004")
    refreshed = session.exec(select(SyncRecord).where(SyncRecord.source_id == "7004")).first()
    assert refreshed.status == SyncStatus.PENDING
    assert refreshed.attempts == 0
