from decimal import Decimal

import pytest

from app.currency.convert import (
    OverRefundError,
    assert_not_over_refunded,
    calculate_refund_portion,
    fx_gain_loss,
    to_base_currency,
)


def test_full_refund_portion_is_one():
    assert calculate_refund_portion(Decimal("100.00"), Decimal("100.00")) == Decimal("1")


def test_partial_refund_portion():
    portion = calculate_refund_portion(Decimal("30.00"), Decimal("100.00"))
    assert portion == Decimal("0.3")


def test_partial_refund_portion_non_round():
    # 33.33 / 100 should not drift due to float error
    portion = calculate_refund_portion(Decimal("33.33"), Decimal("100.00"))
    assert portion == Decimal("33.33") / Decimal("100.00")
    assert str(portion) != "0.333299999999"  # sanity: this is Decimal, not float


def test_refund_exceeding_original_raises():
    with pytest.raises(OverRefundError):
        calculate_refund_portion(Decimal("150.00"), Decimal("100.00"))


def test_zero_or_negative_amounts_rejected():
    with pytest.raises(ValueError):
        calculate_refund_portion(Decimal("0"), Decimal("100.00"))
    with pytest.raises(ValueError):
        calculate_refund_portion(Decimal("10"), Decimal("0"))


def test_two_partial_refunds_then_over_refund_guard():
    original = Decimal("100.00")
    assert_not_over_refunded(refund_amount=Decimal("30.00"), already_refunded=Decimal("0"), original_amount=original)
    assert_not_over_refunded(refund_amount=Decimal("40.00"), already_refunded=Decimal("30.00"), original_amount=original)
    # 30 + 40 = 70 already refunded; a third refund of 50 would total 120 > 100
    with pytest.raises(OverRefundError):
        assert_not_over_refunded(refund_amount=Decimal("50.00"), already_refunded=Decimal("70.00"), original_amount=original)


def test_exact_full_cumulative_refund_is_allowed():
    assert_not_over_refunded(refund_amount=Decimal("100.00"), already_refunded=Decimal("0"), original_amount=Decimal("100.00"))


def test_to_base_currency_conversion():
    assert to_base_currency(Decimal("100.00"), Decimal("1.10")) == Decimal("110.00")
    assert to_base_currency(Decimal("33.33"), Decimal("0.85")) == Decimal("28.33")  # 28.3305 rounds up


def test_fx_gain_loss_positive_when_rate_rises():
    # Booked at 1.00, refunded when the rate has risen to 1.10 => refund costs more in base currency
    gain_loss = fx_gain_loss(refund_amount=Decimal("100.00"), refund_fx_rate=Decimal("1.10"), booking_fx_rate=Decimal("1.00"))
    assert gain_loss == Decimal("10.00")


def test_fx_gain_loss_zero_when_rate_unchanged():
    gain_loss = fx_gain_loss(refund_amount=Decimal("50.00"), refund_fx_rate=Decimal("1.00"), booking_fx_rate=Decimal("1.00"))
    assert gain_loss == Decimal("0.00")


def test_fx_rate_never_rederived_stays_stamped():
    """The refund's own fx_rate is what convert.py uses — a later change to the
    'current' rate must not alter an already-computed conversion."""
    stamped_rate = Decimal("1.2345")
    amount = Decimal("100.00")
    result_at_stamped_rate = to_base_currency(amount, stamped_rate)

    later_current_rate = Decimal("1.9999")  # rate has since moved a lot
    assert to_base_currency(amount, stamped_rate) == result_at_stamped_rate
    assert to_base_currency(amount, later_current_rate) != result_at_stamped_rate
