import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://localhost:5432/inventorysync")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

import pytest
from sqlmodel import Session, select

from app.db import engine, init_db
from app.models import ChannelEvent, Sku


@pytest.fixture(scope="session", autouse=True)
def _init_database():
    init_db()


@pytest.fixture
def sku(request):
    code = f"TEST-{request.node.name}-{os.getpid()}"
    with Session(engine) as session:
        s = Sku(sku_code=code, canonical_qty=100, version=0)
        session.add(s)
        session.commit()
        session.refresh(s)
    yield s
    with Session(engine) as session:
        db_sku = session.exec(select(Sku).where(Sku.sku_code == code)).first()
        if db_sku:
            session.delete(db_sku)
        for ev in session.exec(select(ChannelEvent).where(ChannelEvent.sku_code == code)):
            session.delete(ev)
        session.commit()
