"""Maps a Shopify order payload to a QuickBooks Online SalesReceipt payload."""

from app.config import Settings
from app.sync.fees import build_fee_line, estimate_shopify_fee


def order_to_qbo_receipt(order: dict, *, settings: Settings) -> dict:
    line_items = order.get("line_items", [])
    currency = order.get("currency") or order.get("presentment_currency") or "USD"

    lines = []
    for item in line_items:
        qty = float(item.get("quantity", 1))
        price = float(item.get("price", 0))
        lines.append(
            {
                "Amount": round(qty * price, 2),
                "DetailType": "SalesItemLineDetail",
                "Description": item.get("title", "Shopify item"),
                "SalesItemLineDetail": {
                    "Qty": qty,
                    "UnitPrice": price,
                    "ItemRef": {"value": settings.qbo_income_account_id},
                },
            }
        )

    fee_amount = estimate_shopify_fee(order)
    fee_line = build_fee_line(fee_amount, settings=settings)
    if fee_line:
        lines.append(fee_line)

    receipt = {
        "DocNumber": f"SHOPIFY-{order['id']}",
        "TxnDate": order.get("created_at"),
        "CurrencyRef": {"value": currency},
        "Line": lines,
        "TotalAmt": round(float(order.get("total_price", 0)), 2),
        "PrivateNote": f"source_id=shopify:{order['id']}",
        "DepositToAccountRef": {"value": settings.qbo_clearing_account_id},
    }
    exchange_rate = order.get("exchange_rate")
    if exchange_rate:
        receipt["ExchangeRate"] = float(exchange_rate)

    customer_email = order.get("email") or order.get("contact_email")
    if customer_email:
        receipt["BillEmail"] = {"Address": customer_email}

    return receipt
