"""Maps a Shopify refund payload to a QuickBooks Online RefundReceipt payload.

This module has exactly one job and no other code path: turn a Shopify
refund into a QBO refund of the exact refunded amount, referencing the
original sales receipt. It never creates a positive SalesReceipt — that
would double-book revenue on a refund, which is one of the three ways these
syncs quietly break.
"""

from app.config import Settings


class RefundExceedsOriginalError(ValueError):
    pass


def refund_amount(refund: dict) -> float:
    total = 0.0
    for txn in refund.get("transactions", []) or []:
        total += float(txn.get("amount", 0))
    if total:
        return round(total, 2)
    # Fallback: sum refund_line_items subtotal + tax if no transactions present.
    for item in refund.get("refund_line_items", []) or []:
        total += float(item.get("subtotal", 0)) + float(item.get("total_tax", 0))
    return round(total, 2)


def refund_to_qbo_refund_receipt(
    refund: dict,
    *,
    original_receipt_id: str,
    original_total: float,
    already_refunded: float,
    settings: Settings,
) -> dict:
    amount = refund_amount(refund)
    if amount <= 0:
        raise ValueError("refund amount must be positive")
    if round(already_refunded + amount, 2) > round(original_total, 2) + 0.01:
        raise RefundExceedsOriginalError(
            f"refund {amount} + already_refunded {already_refunded} exceeds order total {original_total}"
        )

    currency = refund.get("currency") or "USD"
    return {
        "DocNumber": f"SHOPIFY-REFUND-{refund['id']}",
        "TxnDate": refund.get("created_at"),
        "CurrencyRef": {"value": currency},
        "TotalAmt": amount,
        "Line": [
            {
                "Amount": amount,
                "DetailType": "SalesItemLineDetail",
                "Description": "Shopify refund",
                "SalesItemLineDetail": {
                    "Qty": 1,
                    "UnitPrice": amount,
                    "ItemRef": {"value": settings.qbo_income_account_id},
                },
            }
        ],
        "PrivateNote": f"source_id=shopify:refund:{refund['id']} original_receipt={original_receipt_id}",
        "DepositToAccountRef": {"value": settings.qbo_clearing_account_id},
    }
