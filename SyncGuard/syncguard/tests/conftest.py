import base64
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

WEBHOOK_SECRET = "test-secret"


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


class FakeQBOClient:
    """Records every call; never touches the network. One instance per test."""

    calls: list[dict] = []
    doc_numbers_seen: set[str] = set()
    _next_id = 1000

    def __init__(self, settings=None):
        pass

    def create_sales_receipt(self, receipt: dict) -> dict:
        FakeQBOClient.calls.append({"type": "sales_receipt", "receipt": receipt})
        FakeQBOClient.doc_numbers_seen.add(receipt["DocNumber"])
        FakeQBOClient._next_id += 1
        return {"Id": str(FakeQBOClient._next_id), "DocNumber": receipt["DocNumber"]}

    def create_refund_receipt(self, refund_receipt: dict) -> dict:
        FakeQBOClient.calls.append({"type": "refund_receipt", "receipt": refund_receipt})
        FakeQBOClient.doc_numbers_seen.add(refund_receipt["DocNumber"])
        FakeQBOClient._next_id += 1
        return {"Id": str(FakeQBOClient._next_id), "DocNumber": refund_receipt["DocNumber"]}

    def close(self):
        pass

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.doc_numbers_seen = set()
        cls._next_id = 1000


@pytest.fixture(autouse=True)
def fake_qbo(monkeypatch):
    FakeQBOClient.reset()
    monkeypatch.setattr("app.worker.QBOClient", FakeQBOClient)
    yield FakeQBOClient
    FakeQBOClient.reset()


@pytest.fixture(autouse=True)
def app_settings(monkeypatch):
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("QBO_INCOME_ACCOUNT_ID", "1")
    monkeypatch.setenv("QBO_FEE_ACCOUNT_ID", "2")
    monkeypatch.setenv("QBO_CLEARING_ACCOUNT_ID", "3")
    from app.config import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def sample_order(order_id: int = 5001, total: str = "100.00") -> dict:
    return {
        "id": order_id,
        "created_at": "2026-01-15T10:00:00-05:00",
        "updated_at": "2026-01-15T10:00:00-05:00",
        "currency": "USD",
        "total_price": total,
        "email": "buyer@example.com",
        "line_items": [{"title": "Widget", "quantity": 2, "price": "50.00"}],
        "shopify_payout_fee": 3.20,
    }


def sample_refund(refund_id: int, order_id: int, amount: str = "50.00") -> dict:
    return {
        "id": refund_id,
        "order_id": order_id,
        "created_at": "2026-01-16T10:00:00-05:00",
        "currency": "USD",
        "transactions": [{"amount": amount}],
        "refund_line_items": [],
    }


@pytest.fixture()
def client(engine, monkeypatch):
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr("app.webhooks.shopify.get_session", lambda: Session(engine))
    monkeypatch.setattr("app.main.get_session", lambda: Session(engine))

    from app.main import app

    with TestClient(app) as c:
        yield c
