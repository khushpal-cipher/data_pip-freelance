"""Refund-portion and multi-currency math.

Two rules make this correct instead of "close enough":

1. refund_portion = refunded_amount / original_amount, computed in Decimal
   (never float) so partial refunds don't accumulate rounding drift.
2. The FX rate used to convert a refund into the books' base currency is the
   rate AT THE MOMENT OF REFUND, stamped once onto RefundRecord.fx_rate and
   never recomputed. A rate looked up "now" from history would silently
   change every past refund's booked value whenever the FX provider revises
   its series — this is exactly the class of bug that makes books wrong.
"""

from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


class OverRefundError(ValueError):
    """Raised when cumulative refunds would exceed the original transaction total."""


def calculate_refund_portion(refunded_amount: Decimal, original_amount: Decimal) -> Decimal:
    """Fraction of the original transaction this refund represents, as a Decimal in (0, 1]."""
    if original_amount <= 0:
        raise ValueError(f"original_amount must be positive, got {original_amount}")
    if refunded_amount <= 0:
        raise ValueError(f"refunded_amount must be positive, got {refunded_amount}")
    if refunded_amount > original_amount:
        raise OverRefundError(
            f"refunded_amount {refunded_amount} exceeds original_amount {original_amount}"
        )
    return refunded_amount / original_amount


def assert_not_over_refunded(
    *, refund_amount: Decimal, already_refunded: Decimal, original_amount: Decimal
) -> None:
    """Cumulative guard: this refund plus everything already refunded must not exceed the total."""
    total = already_refunded + refund_amount
    if total > original_amount:
        raise OverRefundError(
            f"refund {refund_amount} + already_refunded {already_refunded} "
            f"exceeds original total {original_amount}"
        )


def to_base_currency(amount: Decimal, fx_rate: Decimal) -> Decimal:
    """Convert a foreign-currency amount into base currency at the given (stamped) rate."""
    return (amount * fx_rate).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def fx_gain_loss(
    *, refund_amount: Decimal, refund_fx_rate: Decimal, booking_fx_rate: Decimal
) -> Decimal:
    """Realized FX gain/loss on the refunded portion, in base currency.

    Positive => the refund is worth MORE in base currency now than when the
    original sale was booked (an FX loss to the business paying it out);
    negative => an FX gain. Computed once, from the two stamped rates —
    never re-derived from a live rate lookup.
    """
    at_refund_rate = to_base_currency(refund_amount, refund_fx_rate)
    at_booking_rate = to_base_currency(refund_amount, booking_fx_rate)
    return at_refund_rate - at_booking_rate
