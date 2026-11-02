from decimal import Decimal

from minicart.shipping import shipping_cost


def test_digital_goods_ship_free():
    assert shipping_cost(Decimal("0"), Decimal("10")) == Decimal("0.00")


def test_light_parcel():
    assert shipping_cost(Decimal("0.5"), Decimal("20")) == Decimal("4.99")


def test_medium_parcel():
    assert shipping_cost(Decimal("3"), Decimal("20")) == Decimal("8.99")


def test_heavy_parcel():
    assert shipping_cost(Decimal("15"), Decimal("20")) == Decimal("14.99")


def test_very_heavy_parcel_pays_per_extra_kilo():
    assert shipping_cost(Decimal("25"), Decimal("20")) == Decimal("19.99")


def test_partial_extra_kilo_counts_as_a_whole_kilo():
    assert shipping_cost(Decimal("20.2"), Decimal("20")) == Decimal("15.99")


def test_just_under_the_free_shipping_threshold_pays():
    assert shipping_cost(Decimal("3"), Decimal("74.99")) == Decimal("8.99")


def test_above_the_free_shipping_threshold_ships_free():
    assert shipping_cost(Decimal("3"), Decimal("100.00")) == Decimal("0.00")
