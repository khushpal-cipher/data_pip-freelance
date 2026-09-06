import hashlib
import hmac
import json
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


class FakeQBOClient:
    """Records calls instead of making them — no network, deterministic ids."""

    calls: list[dict] = []

    def __init__(self, settings):
        pass

    def find_refund_receipt_by_doc_number(self, doc_number):
        return None

    def create_refund_receipt(self, refund_receipt: dict) -> dict:
        FakeQBOClient.calls.append(refund_receipt)
        return {"Id": f"qbo-{len(FakeQBOClient.calls)}"}

    def close(self):
        pass


@pytest.fixture
def client(session, monkeypatch):
    import app.tasks.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "QBOClient", FakeQBOClient)
    FakeQBOClient.calls = []

    from app.main import app

    with TestClient(app) as c:
        yield c


def _stripe_signed_post(client, path: str, body: dict, secret: str = "test-stripe-secret"):
    raw = json.dumps(body).encode()
    ts = int(time.time())
    signed_payload = f"{ts}.{raw.decode()}"
    sig = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
    return client.post(
        path,
        content=raw,
        headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"},
    )


def _seed_transaction(session, *, source_id: str, amount: str, currency: str = "USD", fx_rate: str = "1"):
    from app.models import Transaction

    txn = Transaction(
        source_system="stripe",
        source_id=source_id,
        amount=Decimal(amount),
        currency=currency,
        fx_rate=Decimal(fx_rate),
        dest_id="qbo-original-1",
    )
    session.add(txn)
    session.commit()
    return txn


def _refund_event(refund_id: str, charge_id: str, amount_cents: int, currency="usd", fx_rate=None):
    obj = {"id": refund_id, "charge": charge_id, "amount": amount_cents, "currency": currency}
    if fx_rate is not None:
        obj["fx_rate"] = fx_rate
    return {"type": "charge.refunded", "data": {"object": obj}}


def test_partial_refund_syncs_with_correct_portion(client, session):
    _seed_transaction(session, source_id="ch_100", amount="100.00")

    resp = _stripe_signed_post(client, "/webhooks/stripe", _refund_event("re_partial", "ch_100", 3000))
    assert resp.status_code == 200
    assert resp.json()["already_queued"] is False

    from app.models import RefundRecord, RefundStatus
    from sqlmodel import select

    record = session.exec(select(RefundRecord).where(RefundRecord.refund_id == "stripe:re_partial")).first()
    assert record.status == RefundStatus.SYNCED
    assert record.refund_portion == Decimal("0.3")
    assert record.dest_id == "qbo-1"
    assert len(FakeQBOClient.calls) == 1


def test_duplicate_webhook_delivery_processes_once(client, session):
    _seed_transaction(session, source_id="ch_200", amount="50.00")

    for _ in range(3):
        resp = _stripe_signed_post(client, "/webhooks/stripe", _refund_event("re_dup", "ch_200", 5000))
        assert resp.status_code == 200

    assert len(FakeQBOClient.calls) == 1  # only the first delivery actually synced


def test_invalid_signature_rejected(client, session):
    _seed_transaction(session, source_id="ch_300", amount="50.00")
    raw = json.dumps(_refund_event("re_bad_sig", "ch_300", 5000)).encode()
    resp = client.post(
        "/webhooks/stripe",
        content=raw,
        headers={"Stripe-Signature": "t=123,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert len(FakeQBOClient.calls) == 0


def test_unmatched_original_transaction_is_held(client, session):
    resp = _stripe_signed_post(client, "/webhooks/stripe", _refund_event("re_orphan", "ch_never_seen", 1000))
    assert resp.status_code == 200

    from app.models import RefundRecord, RefundStatus
    from sqlmodel import select

    record = session.exec(select(RefundRecord).where(RefundRecord.refund_id == "stripe:re_orphan")).first()
    assert record.status == RefundStatus.UNMATCHED
    assert len(FakeQBOClient.calls) == 0


def test_over_refund_across_two_partials_is_rejected(client, session):
    _seed_transaction(session, source_id="ch_400", amount="100.00")

    r1 = _stripe_signed_post(client, "/webhooks/stripe", _refund_event("re_400_a", "ch_400", 7000))
    assert r1.status_code == 200
    r2 = _stripe_signed_post(client, "/webhooks/stripe", _refund_event("re_400_b", "ch_400", 5000))
    assert r2.status_code == 200

    from app.models import RefundRecord, RefundStatus
    from sqlmodel import select

    first = session.exec(select(RefundRecord).where(RefundRecord.refund_id == "stripe:re_400_a")).first()
    second = session.exec(select(RefundRecord).where(RefundRecord.refund_id == "stripe:re_400_b")).first()
    assert first.status == RefundStatus.SYNCED
    assert second.status == RefundStatus.OVER_REFUND
    assert len(FakeQBOClient.calls) == 1


def test_multi_currency_refund_stamps_fx_rate_and_never_rederives(client, session):
    _seed_transaction(session, source_id="ch_500", amount="200.00", currency="EUR", fx_rate="1.00")

    resp = _stripe_signed_post(
        client, "/webhooks/stripe", _refund_event("re_fx", "ch_500", 10000, currency="eur", fx_rate="1.15")
    )
    assert resp.status_code == 200

    from app.models import RefundRecord
    from sqlmodel import select

    record = session.exec(select(RefundRecord).where(RefundRecord.refund_id == "stripe:re_fx")).first()
    assert record.fx_rate == Decimal("1.15")
    assert record.currency == "EUR"
    receipt = FakeQBOClient.calls[0]
    assert Decimal(receipt["ExchangeRate"]) == Decimal("1.15")
    assert "fx_gain_loss=15.00" in receipt["PrivateNote"]  # 100 base at 1.15 minus 100 base at 1.00
