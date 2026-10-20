# minicart

A small shopping-cart pricing library: Decimal money helpers, bulk-quantity tiers, discount codes, tax rules and
weight-based shipping. It is the demo repository for Fixling's evaluation tasks (each task is a realistic bug in
this library plus a hidden test), so keep its tests green on the clean tree.

```python
from decimal import Decimal
from minicart.cart import Cart
from minicart.models import Item

cart = Cart()
cart.add(Item("pen", "Pen", Decimal("1.20")), qty=12)
cart.subtotal()   # Decimal("12.96")  (10+ units get 10% off)
```
