import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app.channels_mock import mock_router
from app.concurrency.locking import SkuNotFound, apply_delta
from app.config import SENTRY_DSN
from app.db import engine, init_db
from app.models import Sku
from app.webhooks.inbound import router as webhooks_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("inventorysync")

if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.1)
    logger.info("Sentry error tracking enabled")


DEMO_SKUS = [
    ("WIDGET-1", 50),
    ("GADGET-2", 30),
    ("GIZMO-3", 100),
]


def _seed_demo_data() -> None:
    with Session(engine) as session:
        if session.exec(select(Sku)).first():
            return
        for code, qty in DEMO_SKUS:
            session.add(Sku(sku_code=code, canonical_qty=qty, version=0))
        session.commit()
        logger.info("seeded %d demo SKUs", len(DEMO_SKUS))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _seed_demo_data()
    yield


app = FastAPI(title="InventorySync", lifespan=lifespan)
app.include_router(webhooks_router)
app.include_router(mock_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stock/{sku_code}")
def get_stock(sku_code: str):
    with Session(engine) as session:
        sku = session.exec(select(Sku).where(Sku.sku_code == sku_code)).first()
        if not sku:
            raise HTTPException(status_code=404, detail="sku not found")
        return sku


def _do_adjust(sku_code: str, delta: int) -> Sku:
    with Session(engine) as session:
        return apply_delta(session, sku_code, delta)


@app.post("/stock/{sku_code}/adjust")
async def adjust_stock(sku_code: str, payload: dict):
    delta = payload["delta"]
    try:
        sku = await run_in_threadpool(_do_adjust, sku_code, delta)
    except SkuNotFound:
        raise HTTPException(status_code=404, detail="sku not found")

    # Manual/admin adjustment — treat it as its own source so it fans out
    # to every real channel, not just the ones that didn't originate it.
    from app.sync.reconcile import fan_out

    await fan_out(sku_code, sku.canonical_qty, exclude_channel="__admin__")

    return sku
