"""Sales-tax rules, including tax-inclusive prices (VAT style)."""

from dataclasses import dataclass, field
from decimal import Decimal

from minicart.models import Item
from minicart.money import round_half_up


@dataclass(frozen=True)
class TaxRule:
    region: str
    rate: Decimal  # 0.08 = 8%
    inclusive: bool = False  # prices already contain the tax
    exempt_categories: frozenset[str] = field(default_factory=frozenset)


def tax_for_amount(amount: Decimal, rule: TaxRule) -> Decimal:
    """Tax on ``amount``. For tax-inclusive prices this is the tax *contained in* ``amount``."""
    if amount <= 0:
        return Decimal("0.00")
    if rule.inclusive:
        return round_half_up(amount - amount / (1 + rule.rate))
    return round_half_up(amount * rule.rate)


def is_taxable(item: Item, rule: TaxRule) -> bool:
    return item.taxable and item.category not in rule.exempt_categories


def net_of_tax(amount: Decimal, rule: TaxRule) -> Decimal:
    """The price without tax, whether or not ``amount`` already includes it."""
    if rule.inclusive:
        return round_half_up(amount - tax_for_amount(amount, rule))
    return round_half_up(amount)
