import logging
import random
import time

from sqlmodel import Session, select, update

from app.models import Sku

logger = logging.getLogger("inventorysync.locking")

MAX_RETRIES = 5


class SkuNotFound(Exception):
    pass


def apply_delta(session: Session, sku_code: str, delta: int) -> Sku:
    """Optimistic-locking write: read version, write WHERE version=read_version.
    On a lost race (rowcount 0) re-read and retry with backoff — this is what
    prevents overselling when two channels report a change at the same instant.
    """
    for attempt in range(MAX_RETRIES):
        sku = session.exec(select(Sku).where(Sku.sku_code == sku_code)).first()
        if sku is None:
            raise SkuNotFound(sku_code)

        new_qty = max(0, sku.canonical_qty + delta)
        result = session.exec(
            update(Sku)
            .where(Sku.id == sku.id, Sku.version == sku.version)
            .values(canonical_qty=new_qty, version=sku.version + 1)
        )
        session.commit()

        if result.rowcount == 1:
            session.refresh(sku)
            return sku

        logger.info(
            "optimistic lock conflict on %s (attempt %d), retrying", sku_code, attempt + 1
        )
        time.sleep(0.01 * (2**attempt) + random.uniform(0, 0.01))

    raise RuntimeError(f"could not apply delta to {sku_code} after {MAX_RETRIES} attempts")
