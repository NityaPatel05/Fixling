"""Discount codes: validation, amounts and stacking."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from minicart.money import round_half_up


@dataclass(frozen=True)
class DiscountCode:
    code: str
    kind: Literal["percent", "fixed"]
    value: Decimal  # percent points (10 = 10%) or a fixed amount
    min_subtotal: Decimal = Decimal("0")
    max_discount: Decimal | None = None  # cap for percent codes
    valid_until: date | None = None  # inclusive: still valid on this day
    stackable: bool = False  # may be combined with other codes


def check_code(code: DiscountCode, subtotal: Decimal, today: date) -> tuple[bool, str]:
    """``(usable, reason)``. A code is valid through the whole of ``valid_until``."""
    if code.valid_until is not None and today > code.valid_until:
        return False, f"{code.code} expired on {code.valid_until.isoformat()}"
    if subtotal < code.min_subtotal:
        return False, f"{code.code} needs a subtotal of at least {code.min_subtotal}"
    return True, ""


def discount_amount(code: DiscountCode, subtotal: Decimal) -> Decimal:
    """How much ``code`` takes off ``subtotal``: never negative and never more than the subtotal."""
    if subtotal <= 0:
        return Decimal("0.00")
    if code.kind == "percent":
        amount = round_half_up(subtotal * code.value / Decimal(100))
        if code.max_discount is not None:
            amount = min(amount, code.max_discount)
    else:
        amount = code.value
    return round_half_up(min(amount, subtotal))


def apply_codes(codes: list[DiscountCode], subtotal: Decimal, today: date) -> tuple[Decimal, list[str]]:
    """Total discount for ``codes`` plus human-readable notes about codes that were refused.

    Valid stackable codes combine, each computed on what is left after the previous one. If any valid code is not
    stackable it can only be used alone: the single best code wins and the others are refused.
    """
    notes: list[str] = []
    valid: list[DiscountCode] = []
    for code in codes:
        ok, reason = check_code(code, subtotal, today)
        if ok:
            valid.append(code)
        else:
            notes.append(reason)
    if len(valid) > 1 and any(not c.stackable for c in valid):
        best = max(valid, key=lambda c: discount_amount(c, subtotal))
        notes += [f"{c.code} cannot be combined with other codes" for c in valid if c is not best]
        valid = [best]
    remaining = subtotal
    total = Decimal("0.00")
    for code in valid:
        amount = discount_amount(code, remaining)
        total += amount
        remaining -= amount
    return round_half_up(min(total, subtotal)), notes
