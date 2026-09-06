"""Splits Shopify payout fees from revenue.

A sales receipt posts at gross (what the customer paid). The Shopify Payments
processing fee is never subtracted from the revenue line — it posts as its
own expense-account line, deposited to a clearing account, exactly like a
real bank statement shows gross-in / fee-out / net-deposit. Netting the fee
into revenue would silently understate top-line sales, which is one of the
three ways these syncs quietly break.
"""

from app.config import Settings


def build_fee_line(fee_amount: float, *, settings: Settings) -> dict | None:
    if not fee_amount:
        return None
    return {
        "Amount": round(fee_amount, 2),
        "DetailType": "SalesItemLineDetail",
        "Description": "Shopify payout processing fee",
        "AccountRef": {"value": settings.qbo_fee_account_id},
    }


def estimate_shopify_fee(order: dict) -> float:
    """Best-effort fee extraction from a Shopify order payload.

    Shopify does not put the payout fee on the order webhook itself (it's on
    the Payout/Transaction object from a separate endpoint); order payloads
    that already carry it (e.g. via `payment_terms`/app-injected fields, or a
    pre-fetched transaction lookup) are read here. Defaults to 0 so a missing
    fee never blocks the sync — reconciliation is what catches drift.
    """
    fee = order.get("shopify_payout_fee")
    if fee is not None:
        return float(fee)
    for txn in order.get("transactions", []) or []:
        fee = txn.get("fee")
        if fee is not None:
            return float(fee)
    return 0.0
