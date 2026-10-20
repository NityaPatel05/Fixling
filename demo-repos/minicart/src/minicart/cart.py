"""The cart: lines, discount codes and the order of operations for totals.

Totals are computed as: subtotal (after bulk tiers) -> discount codes -> tax on the discounted taxable share ->
shipping on the discounted subtotal -> total.
"""

from datetime import date
from decimal import Decimal

from minicart.discounts import DiscountCode, apply_codes
from minicart.models import CartLine, Item
from minicart.money import round_half_up
from minicart.pricing import DEFAULT_TIERS, Tier, line_price
from minicart.shipping import shipping_cost
from minicart.tax import TaxRule, is_taxable, net_of_tax, tax_for_amount

ZERO = Decimal("0.00")


class Cart:
    def __init__(self, tax_rule: TaxRule | None = None, tiers: tuple[Tier, ...] = DEFAULT_TIERS) -> None:
        self.tax_rule = tax_rule
        self.tiers = tiers
        self._lines: dict[str, CartLine] = {}
        self.codes: list[DiscountCode] = []

    # ------------------------------------------------------------------ contents
    @property
    def lines(self) -> list[CartLine]:
        return list(self._lines.values())

    def item_count(self) -> int:
        """Total units in the cart."""
        return sum(line.qty for line in self._lines.values())

    def add(self, item: Item, qty: int = 1) -> None:
        if qty < 1:
            raise ValueError("qty must be at least 1")
        if item.sku in self._lines:
            self._lines[item.sku].qty += qty
        else:
            self._lines[item.sku] = CartLine(item, qty)

    def remove(self, sku: str) -> None:
        if sku not in self._lines:
            raise KeyError(sku)
        del self._lines[sku]

    def update_qty(self, sku: str, qty: int) -> None:
        """Set a line's quantity; zero removes the line."""
        if sku not in self._lines:
            raise KeyError(sku)
        if qty < 0:
            raise ValueError("qty must not be negative")
        if qty == 0:
            del self._lines[sku]
        else:
            self._lines[sku].qty = qty

    def apply_code(self, code: DiscountCode) -> None:
        if all(existing.code != code.code for existing in self.codes):
            self.codes.append(code)

    # ------------------------------------------------------------------ totals
    def line_total(self, line: CartLine) -> Decimal:
        return line_price(line.item, line.qty, self.tiers)

    def subtotal(self) -> Decimal:
        return round_half_up(sum((self.line_total(line) for line in self._lines.values()), ZERO))

    def discount(self, today: date) -> Decimal:
        total, _ = apply_codes(self.codes, self.subtotal(), today)
        return total

    def discount_notes(self, today: date) -> list[str]:
        return apply_codes(self.codes, self.subtotal(), today)[1]

    def taxable_base(self, today: date) -> Decimal:
        """Taxable lines' total after the discount has been spread over the whole cart pro rata."""
        subtotal = self.subtotal()
        if subtotal <= 0 or self.tax_rule is None:
            return ZERO
        taxable = sum(
            (self.line_total(line) for line in self._lines.values() if is_taxable(line.item, self.tax_rule)), ZERO
        )
        share = (subtotal - self.discount(today)) / subtotal
        return round_half_up(taxable * share)

    def tax(self, today: date) -> Decimal:
        if self.tax_rule is None:
            return ZERO
        return tax_for_amount(self.taxable_base(today), self.tax_rule)

    def weight_kg(self) -> Decimal:
        return sum((line.item.weight_kg * line.qty for line in self._lines.values()), Decimal("0"))

    def shipping(self, today: date) -> Decimal:
        return shipping_cost(self.weight_kg(), self.subtotal() - self.discount(today))

    def total(self, today: date) -> Decimal:
        """What the customer pays. Tax-inclusive prices already contain their tax, so it is not added again."""
        subtotal, discount = self.subtotal(), self.discount(today)
        base = subtotal - discount
        if self.tax_rule is not None and not self.tax_rule.inclusive:
            base += self.tax(today)
        return round_half_up(base + self.shipping(today))

    def summary(self, today: date) -> dict[str, Decimal]:
        rule = self.tax_rule
        subtotal = self.subtotal()
        return {
            "subtotal": subtotal,
            "discount": self.discount(today),
            "tax": self.tax(today),
            "shipping": self.shipping(today),
            "total": self.total(today),
            "net_subtotal": net_of_tax(subtotal, rule) if rule is not None else subtotal,
        }
