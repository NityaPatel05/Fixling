"""Weight-based shipping with a free-shipping threshold."""

import math
from decimal import Decimal

# (heaviest weight in the band, inclusive; cost). 1.0 kg still ships at the first band's price.
BANDS = (
    (Decimal("1"), Decimal("4.99")),
    (Decimal("5"), Decimal("8.99")),
    (Decimal("20"), Decimal("14.99")),
)
FREE_SHIPPING_FROM = Decimal("75.00")  # a subtotal of exactly this much already ships free
PER_EXTRA_KG = Decimal("1.00")


def shipping_cost(weight_kg: Decimal, subtotal: Decimal) -> Decimal:
    """Shipping for a parcel of ``weight_kg`` on an order worth ``subtotal`` (after discounts)."""
    if weight_kg <= 0:
        return Decimal("0.00")  # nothing physical to ship
    if subtotal >= FREE_SHIPPING_FROM:
        return Decimal("0.00")
    for heaviest, cost in BANDS:
        if weight_kg <= heaviest:
            return cost
    extra_kg = math.ceil(weight_kg - BANDS[-1][0])
    return BANDS[-1][1] + PER_EXTRA_KG * extra_kg
