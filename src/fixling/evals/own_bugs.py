"""The ten hand-written ``minicart`` bugs (source ``own`` tasks).

Each bug is a small, realistic change to the clean library plus a hidden test the existing suite does not have, so
the bug survives the visible tests exactly as real bugs do. ``build_tasks`` validates every entry mechanically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class OwnBug:
    slug: str
    title: str
    difficulty: Literal["easy", "medium", "hard"]
    split: Literal["dev", "test"]
    edits: list[tuple[str, str, str]]  # (file, text in the clean repo, replacement that introduces the bug)
    issue: str  # what the agent sees
    hidden_test: str  # content of tests/hidden/test_<slug>.py
    extra: dict[str, str] = field(default_factory=dict)


OWN_BUGS: list[OwnBug] = [
    OwnBug(
        slug="round_half_ties",
        title="Exact half-cents are rounded the wrong way",
        difficulty="medium",
        split="dev",
        edits=[
            (
                "src/minicart/money.py",
                "from decimal import ROUND_HALF_UP, Decimal",
                "from decimal import ROUND_HALF_EVEN, Decimal",
            ),
            ("src/minicart/money.py", "rounding=ROUND_HALF_UP", "rounding=ROUND_HALF_EVEN"),
        ],
        issue="""Tax comes out one cent too low on some receipts.

An item priced 53.30 with a 5% tax rate has tax of exactly 2.665, which our receipts should show as 2.67. We print
2.66. The same thing happens for 0.125 (we show 0.12, it should be 0.13) and 1.005 (we show 1.00, it should be 1.01).

    >>> from decimal import Decimal
    >>> from minicart.money import round_half_up
    >>> round_half_up(Decimal("2.665"))
    Decimal('2.66')

The function is called round_half_up, so halves should always go up (away from zero).""",
        hidden_test="""from decimal import Decimal

from minicart.money import round_half_up


def test_a_tie_rounds_away_from_zero():
    assert round_half_up(Decimal("2.665")) == Decimal("2.67")


def test_other_ties_round_up_too():
    assert round_half_up(Decimal("0.125")) == Decimal("0.13")
    assert round_half_up(Decimal("1.005")) == Decimal("1.01")


def test_negative_ties_round_away_from_zero():
    assert round_half_up(Decimal("-0.125")) == Decimal("-0.13")
""",
    ),
    OwnBug(
        slug="bulk_tier_boundary",
        title="Ordering exactly 10 units gets no bulk discount",
        difficulty="easy",
        split="dev",
        edits=[("src/minicart/pricing.py", "if qty >= tier.min_qty and", "if qty > tier.min_qty and")],
        issue="""Bulk pricing starts one unit too late.

Our price list says "10 or more units: 10% off" and "50 or more: 20% off". A customer who orders exactly 10 pens at
2.00 each is charged 20.00 instead of 18.00. Exactly 50 units don't get the 20% tier either; they are charged at the
10% tier. 11 and 51 units are priced correctly.""",
        hidden_test="""from decimal import Decimal

from minicart.models import Item
from minicart.pricing import line_price, tier_factor


def test_exactly_ten_units_get_the_first_tier():
    assert tier_factor(10) == Decimal("0.90")


def test_exactly_fifty_units_get_the_second_tier():
    assert tier_factor(50) == Decimal("0.80")


def test_line_price_at_the_first_boundary():
    assert line_price(Item("pen", "Pen", Decimal("2.00")), 10) == Decimal("18.00")
""",
    ),
    OwnBug(
        slug="percent_cap_ignored",
        title="max_discount on percentage codes is ignored",
        difficulty="medium",
        split="test",
        edits=[
            (
                "src/minicart/discounts.py",
                """        amount = round_half_up(subtotal * code.value / Decimal(100))
        if code.max_discount is not None:
            amount = min(amount, code.max_discount)
""",
                """        amount = round_half_up(subtotal * code.value / Decimal(100))
""",
            )
        ],
        issue="""A "50% off, up to 20" promotion gave away much more than 20.

We created DiscountCode("HALFOFF", "percent", Decimal("50"), max_discount=Decimal("20")). On a 200.00 order the
customer got 100.00 off instead of 20.00. Smaller orders look fine (a 30.00 order correctly gets 15.00 off).""",
        hidden_test="""from decimal import Decimal

from minicart.discounts import DiscountCode, discount_amount

HALF = DiscountCode("HALFOFF", "percent", Decimal("50"), max_discount=Decimal("20"))


def test_a_percent_code_is_capped():
    assert discount_amount(HALF, Decimal("200.00")) == Decimal("20.00")


def test_the_cap_does_not_matter_below_it():
    assert discount_amount(HALF, Decimal("30.00")) == Decimal("15.00")


def test_a_cap_equal_to_the_discount_is_fine():
    assert discount_amount(HALF, Decimal("40.00")) == Decimal("20.00")
""",
    ),
    OwnBug(
        slug="fixed_discount_over_subtotal",
        title="A fixed discount can be larger than the order",
        difficulty="easy",
        split="test",
        edits=[
            ("src/minicart/discounts.py", "return round_half_up(min(amount, subtotal))", "return round_half_up(amount)")
        ],
        issue="""discount_amount() returns more than the order is worth for fixed codes.

With a 50.00 fixed code on a 12.00 order I expected a discount of 12.00 (you can't discount more than the order), but
discount_amount returns 50.00. Totals shown by the cart are fine, but anything that uses discount_amount directly (our
reporting export) shows nonsense.""",
        hidden_test="""from decimal import Decimal

from minicart.discounts import DiscountCode, discount_amount

BIG = DiscountCode("BIG", "fixed", Decimal("50"))


def test_a_fixed_discount_is_limited_to_the_subtotal():
    assert discount_amount(BIG, Decimal("12.00")) == Decimal("12.00")


def test_a_fixed_discount_below_the_subtotal_is_unchanged():
    assert discount_amount(BIG, Decimal("80.00")) == Decimal("50.00")
""",
    ),
    OwnBug(
        slug="free_shipping_threshold",
        title="An order of exactly 75.00 still pays shipping",
        difficulty="easy",
        split="dev",
        edits=[("src/minicart/shipping.py", "if subtotal >= FREE_SHIPPING_FROM:", "if subtotal > FREE_SHIPPING_FROM:")],
        issue="""Free shipping should start at 75.00 but it starts a cent later.

The shop banner says "Free shipping on orders from 75.00". A customer whose cart came to exactly 75.00 was still
charged 8.99 for shipping. 75.01 ships free as expected.""",
        hidden_test="""from decimal import Decimal

from minicart.shipping import shipping_cost


def test_exactly_the_threshold_ships_free():
    assert shipping_cost(Decimal("2"), Decimal("75.00")) == Decimal("0.00")


def test_a_cent_below_still_pays():
    assert shipping_cost(Decimal("2"), Decimal("74.99")) == Decimal("8.99")
""",
    ),
    OwnBug(
        slug="inclusive_tax_amount",
        title="Tax-inclusive prices report far too much tax",
        difficulty="medium",
        split="test",
        edits=[
            (
                "src/minicart/tax.py",
                "return round_half_up(amount - amount / (1 + rule.rate))",
                "return round_half_up(amount * rule.rate)",
            )
        ],
        issue="""VAT-style regions (prices include tax) show the wrong tax and net price.

For a 120.00 item in a region with 20% VAT the receipt says "tax 24.00, net 96.00". The correct split is tax 20.00 and
net 100.00 (120 / 1.2 = 100). The total charged (120.00) is right; only the breakdown is wrong.""",
        hidden_test="""from decimal import Decimal

from minicart.cart import Cart
from minicart.models import Item
from minicart.tax import TaxRule, net_of_tax, tax_for_amount

EU = TaxRule("EU", Decimal("0.20"), inclusive=True)


def test_inclusive_tax_is_the_share_contained_in_the_price():
    assert tax_for_amount(Decimal("120.00"), EU) == Decimal("20.00")


def test_net_of_tax_removes_it():
    assert net_of_tax(Decimal("120.00"), EU) == Decimal("100.00")


def test_cart_breakdown_for_an_inclusive_region():
    from datetime import date

    cart = Cart(EU)
    cart.add(Item("lamp", "Lamp", Decimal("120.00")))
    summary = cart.summary(date(2026, 3, 15))
    assert summary["tax"] == Decimal("20.00") and summary["net_subtotal"] == Decimal("100.00")
    assert summary["total"] == Decimal("120.00")
""",
    ),
    OwnBug(
        slug="update_qty_zero",
        title="Setting a quantity to 0 leaves an empty line in the cart",
        difficulty="easy",
        split="dev",
        edits=[
            (
                "src/minicart/cart.py",
                """        if qty == 0:
            del self._lines[sku]
        else:
            self._lines[sku].qty = qty
""",
                """        self._lines[sku].qty = qty
""",
            )
        ],
        issue="""update_qty(sku, 0) should remove the line but it keeps a row with quantity 0.

After cart.update_qty("pen", 0) the cart page still lists "Pen x 0" and cart.lines still has one entry. The docstring
says "zero removes the line", and remove() works as expected.""",
        hidden_test="""from decimal import Decimal

from minicart.cart import Cart
from minicart.models import Item

PEN = Item("pen", "Pen", Decimal("1.20"))


def test_setting_zero_removes_the_line():
    cart = Cart()
    cart.add(PEN, 3)
    cart.update_qty("pen", 0)
    assert cart.lines == []
    assert cart.item_count() == 0


def test_other_lines_are_untouched():
    cart = Cart()
    cart.add(PEN, 3)
    cart.add(Item("ink", "Ink", Decimal("4.00")))
    cart.update_qty("pen", 0)
    assert [line.item.sku for line in cart.lines] == ["ink"]
""",
    ),
    OwnBug(
        slug="stacking_mixed_codes",
        title="A non-stackable code can be combined with a stackable one",
        difficulty="medium",
        split="test",
        edits=[
            ("src/minicart/discounts.py", "any(not c.stackable for c in valid)", "all(not c.stackable for c in valid)")
        ],
        issue="""Customers are stacking promotions that must not be combined.

Codes marked stackable=False are supposed to be used alone. With one stackable 10% code (SPRING) and one
non-stackable 20% code (VIP) on a 100.00 order, the customer got 28.00 off (both applied). Only the better code
(VIP, 20.00) should apply; SPRING should be refused with a note that it can't be combined.""",
        hidden_test="""from datetime import date
from decimal import Decimal

from minicart.discounts import DiscountCode, apply_codes

TODAY = date(2026, 3, 15)
SPRING = DiscountCode("SPRING", "percent", Decimal("10"), stackable=True)
VIP = DiscountCode("VIP", "percent", Decimal("20"))


def test_a_non_stackable_code_is_used_alone():
    total, notes = apply_codes([SPRING, VIP], Decimal("100"), TODAY)
    assert total == Decimal("20.00")
    assert any("SPRING" in n for n in notes)


def test_order_of_the_codes_does_not_matter():
    assert apply_codes([VIP, SPRING], Decimal("100"), TODAY)[0] == Decimal("20.00")


def test_the_better_code_wins_even_if_it_is_the_stackable_one():
    big = DiscountCode("BIG", "percent", Decimal("30"), stackable=True)
    total, _ = apply_codes([big, VIP], Decimal("100"), TODAY)
    assert total == Decimal("30.00")
""",
    ),
    OwnBug(
        slug="code_valid_through_expiry_day",
        title="Discount codes stop working a day early",
        difficulty="easy",
        split="dev",
        edits=[("src/minicart/discounts.py", "today > code.valid_until", "today >= code.valid_until")],
        issue="""A code that is valid "through March 31" is rejected on March 31.

DiscountCode("SPRING", ..., valid_until=date(2026, 3, 31)) works on the 30th but on the 31st check_code says it
expired on 2026-03-31. Our marketing copy promises the code works through the end of that day.""",
        hidden_test="""from datetime import date
from decimal import Decimal

from minicart.discounts import DiscountCode, check_code

SPRING = DiscountCode("SPRING", "percent", Decimal("10"), valid_until=date(2026, 3, 31))


def test_valid_on_the_last_day():
    assert check_code(SPRING, Decimal("50"), date(2026, 3, 31)) == (True, "")


def test_expired_the_day_after():
    ok, reason = check_code(SPRING, Decimal("50"), date(2026, 4, 1))
    assert not ok and "expired" in reason
""",
    ),
    OwnBug(
        slug="shipping_band_boundary",
        title="A parcel of exactly 1 kg is charged the 5 kg price",
        difficulty="easy",
        split="test",
        edits=[("src/minicart/shipping.py", "if weight_kg <= heaviest:", "if weight_kg < heaviest:")],
        issue="""Shipping bands include their upper limit, but the limit is treated as exclusive.

A 1.0 kg parcel costs 8.99 (the 1-5 kg band) instead of 4.99. The same happens at exactly 5 kg (14.99 instead of
8.99) and at exactly 20 kg. Parcels slightly under the limit (0.99 kg, 4.99 kg) are correct.""",
        hidden_test="""from decimal import Decimal

from minicart.shipping import shipping_cost

SUBTOTAL = Decimal("10.00")


def test_one_kilo_is_in_the_first_band():
    assert shipping_cost(Decimal("1"), SUBTOTAL) == Decimal("4.99")


def test_five_kilos_is_in_the_second_band():
    assert shipping_cost(Decimal("5"), SUBTOTAL) == Decimal("8.99")


def test_twenty_kilos_is_in_the_third_band():
    assert shipping_cost(Decimal("20"), SUBTOTAL) == Decimal("14.99")
""",
    ),
]
