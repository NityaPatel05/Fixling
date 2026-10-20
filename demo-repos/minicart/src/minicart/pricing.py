"""Unit prices and bulk-quantity tiers."""

from dataclasses import dataclass
from decimal import Decimal

from minicart.models import Item
from minicart.money import round_half_up


@dataclass(frozen=True)
class Tier:
    """Orders of at least ``min_qty`` units pay ``factor`` of the list price (0.90 = 10% off)."""

    min_qty: int
    factor: Decimal


DEFAULT_TIERS = (Tier(10, Decimal("0.90")), Tier(50, Decimal("0.80")))


def tier_factor(qty: int, tiers: tuple[Tier, ...] = DEFAULT_TIERS) -> Decimal:
    """Price factor for ``qty`` units: the best (highest ``min_qty``) tier that the quantity reaches."""
    factor = Decimal("1")
    reached = 0
    for tier in tiers:
        if qty >= tier.min_qty and tier.min_qty >= reached:
            factor = tier.factor
            reached = tier.min_qty
    return factor


def line_price(item: Item, qty: int, tiers: tuple[Tier, ...] = DEFAULT_TIERS) -> Decimal:
    """Total for ``qty`` units of ``item`` after the bulk tier, rounded to cents."""
    if qty < 0:
        raise ValueError("qty must not be negative")
    return round_half_up(item.unit_price * qty * tier_factor(qty, tiers))
