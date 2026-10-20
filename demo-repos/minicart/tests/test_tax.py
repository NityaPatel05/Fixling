from decimal import Decimal

from minicart.models import Item
from minicart.tax import TaxRule, is_taxable, net_of_tax, tax_for_amount

US = TaxRule("US-CA", Decimal("0.08"))


def test_exclusive_tax():
    assert tax_for_amount(Decimal("100.00"), US) == Decimal("8.00")


def test_exclusive_tax_rounds_to_cents():
    assert tax_for_amount(Decimal("19.99"), US) == Decimal("1.60")


def test_no_tax_on_zero_or_negative_amounts():
    assert tax_for_amount(Decimal("0"), US) == Decimal("0.00")
    assert tax_for_amount(Decimal("-5"), US) == Decimal("0.00")


def test_zero_rate_region():
    assert tax_for_amount(Decimal("100.00"), TaxRule("US-OR", Decimal("0"))) == Decimal("0.00")


def test_taxable_item():
    assert is_taxable(Item("pen", "Pen", Decimal("1")), US)


def test_non_taxable_item():
    assert not is_taxable(Item("gift", "Gift card", Decimal("25"), taxable=False), US)


def test_exempt_category():
    rule = TaxRule("US-NY", Decimal("0.08"), exempt_categories=frozenset({"groceries"}))
    assert not is_taxable(Item("apple", "Apple", Decimal("1"), category="groceries"), rule)
    assert is_taxable(Item("pen", "Pen", Decimal("1")), rule)


def test_net_of_tax_for_exclusive_prices_is_unchanged():
    assert net_of_tax(Decimal("100.00"), US) == Decimal("100.00")
