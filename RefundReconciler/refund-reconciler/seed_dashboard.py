"""Seed the dashboard with sample activity against the real DATABASE_URL (.env).

    python seed_dashboard.py

Inserts a handful of original transactions plus refunds in a spread of
statuses (synced, unmatched, over_refund, failed/dead-lettered) so
/dashboard has something to show. Does not touch Redis or Celery — it writes
ledger rows directly, the same shape the reconcile task would leave behind.

Safe to run repeatedly: it clears its own seed rows first. (session.merge()
would not do this — merge matches on the primary key, which is None on a
fresh object, so every run would attempt a new INSERT and collide with the
natural unique key on the second run.)
"""

from decimal import Decimal

from sqlalchemy import delete

from app.db import get_session, init_db
from app.models import RefundRecord, RefundStatus, Transaction, utcnow

init_db()
session = get_session()

transactions = [
    Transaction(source_system="stripe", source_id="ch_9001", amount=Decimal("120.00"), currency="USD", fx_rate=Decimal("1"), dest_id="qbo-9001"),
    Transaction(source_system="stripe", source_id="ch_9002", amount=Decimal("450.00"), currency="EUR", fx_rate=Decimal("1.02"), dest_id="qbo-9002"),
    Transaction(source_system="shopify", source_id="5551001", amount=Decimal("89.99"), currency="USD", fx_rate=Decimal("1"), dest_id="qbo-9003"),
]
refunds = [
    RefundRecord(
        refund_id="stripe:re_seed_1", source_system="stripe", original_txn_id="ch_9001",
        amount=Decimal("40.00"), currency="USD", fx_rate=Decimal("1"), refund_portion=Decimal("0.333333"),
        status=RefundStatus.SYNCED, dest_id="qbo-refund-1", processed_at=utcnow(),
    ),
    RefundRecord(
        refund_id="stripe:re_seed_2", source_system="stripe", original_txn_id="ch_9002",
        amount=Decimal("225.00"), currency="EUR", fx_rate=Decimal("1.09"), refund_portion=Decimal("0.5"),
        status=RefundStatus.SYNCED, dest_id="qbo-refund-2", processed_at=utcnow(),
    ),
    RefundRecord(
        refund_id="shopify:re_seed_3", source_system="shopify", original_txn_id="5551001",
        amount=Decimal("89.99"), currency="USD", fx_rate=Decimal("1"), refund_portion=Decimal("1"),
        status=RefundStatus.SYNCED, dest_id="qbo-refund-3", processed_at=utcnow(),
    ),
    RefundRecord(
        refund_id="stripe:re_seed_orphan", source_system="stripe", original_txn_id="ch_never_booked",
        amount=Decimal("15.00"), currency="USD", fx_rate=Decimal("1"),
        status=RefundStatus.UNMATCHED, last_error="no original transaction found for stripe:ch_never_booked",
    ),
    RefundRecord(
        refund_id="stripe:re_seed_over", source_system="stripe", original_txn_id="ch_9001",
        amount=Decimal("100.00"), currency="USD", fx_rate=Decimal("1"),
        status=RefundStatus.OVER_REFUND, last_error="refund 100.00 + already_refunded 40.00 exceeds original total 120.00",
    ),
    RefundRecord(
        refund_id="stripe:re_seed_failed", source_system="stripe", original_txn_id="ch_9002",
        amount=Decimal("50.00"), currency="EUR", fx_rate=Decimal("1.09"),
        status=RefundStatus.FAILED, attempts=6, last_error="QBO returned 503 Service Unavailable",
    ),
]
# Clear this script's own rows first so it can be run any number of times.
session.execute(delete(RefundRecord).where(RefundRecord.refund_id.in_([r.refund_id for r in refunds])))
session.execute(delete(Transaction).where(Transaction.source_id.in_([t.source_id for t in transactions])))
session.commit()

session.add_all(transactions)
session.add_all(refunds)
session.commit()
session.close()

print(f"Seeded {len(transactions)} transactions and {len(refunds)} refund records.")
print("Start the app (`uvicorn app.main:app --port 8010`), then open /dashboard on that port.")
