from datetime import date
from decimal import Decimal

import pytest

from minicart.cart import Cart
from minicart.discounts import DiscountCode
from minicart.models import Item
from minicart.tax import TaxRule

TODAY = date(2026, 3, 15)
PEN = Item("pen", "Pen", Decimal("1.20"))
BOOK = Item("book", "Book", Decimal("20.00"), weight_kg=Decimal("0.4"))
GIFT = Item("gift", "Gift card", Decimal("25.00"), taxable=False)
US = TaxRule("US-CA", Decimal("0.08"))


def test_new_cart_is_empty():
    cart = Cart()
    assert cart.lines == [] and cart.item_count() == 0 and cart.subtotal() == Decimal("0.00")


def test_add_item():
    cart = Cart()
    cart.add(PEN, 3)
    assert cart.item_count() == 3 and cart.subtotal() == Decimal("3.60")


def test_adding_the_same_sku_merges_lines():
    cart = Cart()
    cart.add(PEN, 2)
    cart.add(PEN, 3)
    assert len(cart.lines) == 1 and cart.item_count() == 5


def test_add_rejects_non_positive_quantity():
    with pytest.raises(ValueError):
        Cart().add(PEN, 0)


def test_remove_line():
    cart = Cart()
    cart.add(PEN)
    cart.remove("pen")
    assert cart.lines == []


def test_remove_unknown_sku():
    with pytest.raises(KeyError):
        Cart().remove("nope")


def test_update_quantity():
    cart = Cart()
    cart.add(PEN, 2)
    cart.update_qty("pen", 5)
    assert cart.item_count() == 5


def test_update_quantity_rejects_negative_and_unknown():
    cart = Cart()
    cart.add(PEN)
    with pytest.raises(ValueError):
        cart.update_qty("pen", -2)
    with pytest.raises(KeyError):
        cart.update_qty("nope", 1)


def test_bulk_tier_applies_in_the_cart():
    cart = Cart()
    cart.add(PEN, 12)
    assert cart.subtotal() == Decimal("12.96")


def test_subtotal_sums_lines():
    cart = Cart()
    cart.add(PEN, 5)
    cart.add(BOOK, 2)
    assert cart.subtotal() == Decimal("46.00")


def test_discount_code_reduces_the_total():
    cart = Cart()
    cart.add(BOOK, 2)
    cart.apply_code(DiscountCode("SAVE10", "percent", Decimal("10")))
    assert cart.discount(TODAY) == Decimal("4.00")
    assert cart.total(TODAY) == Decimal("40.00") - Decimal("4.00") + Decimal("4.99")


def test_the_same_code_is_only_added_once():
    cart = Cart()
    cart.add(BOOK)
    code = DiscountCode("SAVE10", "percent", Decimal("10"))
    cart.apply_code(code)
    cart.apply_code(code)
    assert len(cart.codes) == 1


def test_tax_on_taxable_lines_only():
    cart = Cart(US)
    cart.add(BOOK, 2)
    cart.add(GIFT)
    assert cart.tax(TODAY) == Decimal("3.20")


def test_no_tax_rule_means_no_tax():
    cart = Cart()
    cart.add(BOOK)
    assert cart.tax(TODAY) == Decimal("0.00")


def test_discount_reduces_the_taxable_base_pro_rata():
    cart = Cart(US)
    cart.add(BOOK, 5)  # 100.00, all taxable
    cart.apply_code(DiscountCode("SAVE10", "percent", Decimal("10")))
    assert cart.taxable_base(TODAY) == Decimal("90.00")
    assert cart.tax(TODAY) == Decimal("7.20")


def test_total_adds_exclusive_tax_and_shipping():
    cart = Cart(US)
    cart.add(BOOK, 2)  # 40.00, 1.0 kg
    assert cart.total(TODAY) == Decimal("40.00") + Decimal("3.20") + Decimal("4.99")


def test_free_shipping_on_a_big_order():
    cart = Cart()
    cart.add(BOOK, 6)  # 120.00
    assert cart.shipping(TODAY) == Decimal("0.00")


def test_shipping_uses_the_discounted_subtotal():
    cart = Cart()
    cart.add(BOOK, 5)  # 100.00, 2.5 kg
    cart.apply_code(DiscountCode("HALF", "percent", Decimal("50")))
    assert cart.shipping(TODAY) == Decimal("8.99")  # 50.00 after the code: below the free-shipping threshold


def test_summary_has_all_parts():
    cart = Cart(US)
    cart.add(BOOK, 2)
    summary = cart.summary(TODAY)
    assert set(summary) == {"subtotal", "discount", "tax", "shipping", "total", "net_subtotal"}
    assert summary["subtotal"] == Decimal("40.00") and summary["net_subtotal"] == Decimal("40.00")


def test_discount_notes_report_refused_codes():
    cart = Cart()
    cart.add(BOOK, 2)
    cart.apply_code(DiscountCode("OLD", "percent", Decimal("10"), valid_until=date(2020, 1, 1)))
    assert "expired" in cart.discount_notes(TODAY)[0]
