"""Plain data holders for cart contents."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    unit_price: Decimal
    weight_kg: Decimal = Decimal("0")
    taxable: bool = True
    category: str = "general"


@dataclass
class CartLine:
    item: Item
    qty: int
