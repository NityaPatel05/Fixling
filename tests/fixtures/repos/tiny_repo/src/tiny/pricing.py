"""Pricing helpers for the tiny demo cart."""

BULK_THRESHOLD = 10
BULK_DISCOUNT_PCT = 10


def bulk_price(qty: int, unit_price: float) -> float:
    """Total for ``qty`` units; orders of ``BULK_THRESHOLD`` or more get ``BULK_DISCOUNT_PCT`` % off."""
    total = qty * unit_price
    if qty > BULK_THRESHOLD:
        total = total - total * BULK_DISCOUNT_PCT / 100
    return round(total, 2)


def apply_coupon(total: float, percent: float) -> float:
    """Take ``percent`` % off ``total``; ``percent`` is clamped to 0..100."""
    percent = max(0.0, min(100.0, percent))
    return round(total - total * percent / 100, 2)
