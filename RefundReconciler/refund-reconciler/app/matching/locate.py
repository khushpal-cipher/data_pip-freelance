"""Locate the original transaction a refund event applies against.

Matching is a straight lookup against the stored source_id mapping
(Transaction.source_id) — no fuzzy matching, no heuristics. A refund that
references an original_txn_id we've never booked is held as UNMATCHED for a
human rather than guessed at; guessing here is how books get silently wrong.
"""

from typing import Optional

from sqlmodel import Session, select

from app.models import Transaction


def find_original(session: Session, *, source_system: str, original_txn_id: str) -> Optional[Transaction]:
    if not original_txn_id:
        return None
    return session.exec(
        select(Transaction).where(
            Transaction.source_system == source_system,
            Transaction.source_id == original_txn_id,
        )
    ).first()
