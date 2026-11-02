"""Decimal money helpers. All amounts in minicart are ``Decimal`` rounded to cents."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(value: "int | float | str | Decimal") -> Decimal:
    """Convert to ``Decimal`` without binary-float artefacts (floats go through their repr)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def round_half_up(value: Decimal, places: int = 2) -> Decimal:
    """Round to ``places`` decimals, halves away from zero (2.675 -> 2.68, like a shop receipt)."""
    quantum = Decimal(1).scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def format_money(amount: Decimal, symbol: str = "$") -> str:
    """``$1,234.50``; negative amounts render as ``-$5.00``."""
    rounded = round_half_up(amount)
    sign = "-" if rounded < 0 else ""
    return f"{sign}{symbol}{abs(rounded):,.2f}"


def split_evenly(amount: Decimal, parts: int) -> list[Decimal]:
    """Split ``amount`` into ``parts`` cent-exact shares that add back up to ``amount`` (extra cents go first)."""
    if parts < 1:
        raise ValueError("parts must be at least 1")
    total_cents = int((round_half_up(amount) / CENT).to_integral_value())
    base, extra = divmod(total_cents, parts)
    return [(Decimal(base + (1 if i < extra else 0)) * CENT) for i in range(parts)]
