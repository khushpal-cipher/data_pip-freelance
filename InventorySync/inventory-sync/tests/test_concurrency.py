"""The trust-builder test: fire many concurrent updates at the same SKU and
prove optimistic locking + retry means none of them get lost."""

import threading

from sqlmodel import Session, select

from app.concurrency.locking import apply_delta
from app.db import engine
from app.models import Sku


def test_concurrent_updates_no_lost_update(sku):
    deltas = [-3, -5, -2, -10, -1, -4, -6, -2, -3, -1]  # sums to -37
    errors = []

    def worker(delta):
        try:
            with Session(engine) as session:
                apply_delta(session, sku.sku_code, delta)
        except Exception as exc:  # pragma: no cover - failure path surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(d,)) for d in deltas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker errors: {errors}"

    with Session(engine) as session:
        final = session.exec(select(Sku).where(Sku.sku_code == sku.sku_code)).first()

    assert final.canonical_qty == sku.canonical_qty + sum(deltas)
    assert final.version == sku.version + len(deltas)


def test_qty_never_goes_negative(sku):
    def apply_delta_safe(sku_code):
        with Session(engine) as session:
            apply_delta(session, sku_code, -1000)

    threads = [threading.Thread(target=apply_delta_safe, args=(sku.sku_code,)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with Session(engine) as session:
        final = session.exec(select(Sku).where(Sku.sku_code == sku.sku_code)).first()

    assert final.canonical_qty == 0
