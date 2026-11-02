from decimal import Decimal

import pytest

from minicart.models import Item
from minicart.pricing import DEFAULT_TIERS, Tier, line_price, tier_factor

PEN = Item("pen", "Pen", Decimal("1.20"))


def test_single_unit_has_no_discount():
    assert tier_factor(1) == Decimal("1")


def test_small_orders_have_no_discount():
    assert tier_factor(9) == Decimal("1")


def test_first_tier_applies_above_its_minimum():
    assert tier_factor(11) == Decimal("0.90")
    assert tier_factor(25) == Decimal("0.90")


def test_second_tier_applies_above_its_minimum():
    assert tier_factor(75) == Decimal("0.80")


def test_just_below_second_tier_is_still_first_tier():
    assert tier_factor(49) == Decimal("0.90")


def test_zero_quantity_has_no_discount():
    assert tier_factor(0) == Decimal("1")


def test_custom_tiers():
    tiers = (Tier(5, Decimal("0.50")),)
    assert tier_factor(6, tiers) == Decimal("0.50")
    assert tier_factor(2, tiers) == Decimal("1")


def test_tier_order_does_not_matter():
    tiers = (Tier(50, Decimal("0.80")), Tier(10, Decimal("0.90")))
    assert tier_factor(60, tiers) == Decimal("0.80")
    assert tier_factor(20, tiers) == Decimal("0.90")


def test_line_price_without_tier():
    assert line_price(PEN, 5) == Decimal("6.00")


def test_line_price_with_tier():
    assert line_price(PEN, 12) == Decimal("12.96")


def test_line_price_rounds_to_cents():
    item = Item("x", "X", Decimal("0.333"))
    assert line_price(item, 3) == Decimal("1.00")


def test_line_price_zero_quantity():
    assert line_price(PEN, 0) == Decimal("0.00")


def test_line_price_rejects_negative_quantity():
    with pytest.raises(ValueError):
        line_price(PEN, -1)


def test_default_tiers_are_ordered():
    assert [t.min_qty for t in DEFAULT_TIERS] == sorted(t.min_qty for t in DEFAULT_TIERS)
