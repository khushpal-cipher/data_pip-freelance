from app.config import get_settings
from app.sync.order_to_receipt import order_to_qbo_receipt
from tests.conftest import sample_order


def test_fee_posts_as_separate_line_not_netted_from_revenue():
    settings = get_settings()
    order = sample_order(order_id=6001, total="100.00")
    order["shopify_payout_fee"] = 3.20

    receipt = order_to_qbo_receipt(order, settings=settings)

    fee_lines = [l for l in receipt["Line"] if l.get("AccountRef", {}).get("value") == settings.qbo_fee_account_id]

    assert len(fee_lines) == 1
    assert fee_lines[0]["Amount"] == 3.20
    assert fee_lines[0]["AccountRef"]["value"] == settings.qbo_fee_account_id

    # TotalAmt on the receipt is still the customer-paid gross, not gross-minus-fee
    assert receipt["TotalAmt"] == 100.00


def test_receipt_with_zero_fee_has_no_fee_line():
    settings = get_settings()
    order = sample_order(order_id=6002, total="50.00")
    order.pop("shopify_payout_fee", None)

    receipt = order_to_qbo_receipt(order, settings=settings)
    fee_lines = [l for l in receipt["Line"] if l.get("AccountRef", {}).get("value") == settings.qbo_fee_account_id]
    assert fee_lines == []


def test_multi_currency_order_carries_exchange_rate():
    settings = get_settings()
    order = sample_order(order_id=6003, total="88.00")
    order["currency"] = "EUR"
    order["exchange_rate"] = 1.08

    receipt = order_to_qbo_receipt(order, settings=settings)
    assert receipt["CurrencyRef"]["value"] == "EUR"
    assert receipt["ExchangeRate"] == 1.08
