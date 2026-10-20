from decimal import Decimal

import pytest

from minicart.money import format_money, round_half_up, split_evenly, to_money


def test_to_money_from_int_and_string():
    assert to_money(5) == Decimal("5")
    assert to_money("19.99") == Decimal("19.99")


def test_to_money_avoids_float_artefacts():
    assert to_money(0.1) == Decimal("0.1")
    assert to_money(2.3) == Decimal("2.3")


def test_to_money_keeps_decimals():
    value = Decimal("1.005")
    assert to_money(value) is value


def test_round_half_up_rounds_down_below_the_midpoint():
    assert round_half_up(Decimal("3.14159")) == Decimal("3.14")


def test_round_half_up_rounds_up_above_the_midpoint():
    assert round_half_up(Decimal("3.14659")) == Decimal("3.15")


def test_round_half_up_negative_values():
    assert round_half_up(Decimal("-3.14159")) == Decimal("-3.14")


def test_round_half_up_other_places():
    assert round_half_up(Decimal("7.12345"), 3) == Decimal("7.123")
    assert round_half_up(Decimal("7.89"), 0) == Decimal("8")


def test_round_half_up_keeps_two_places():
    assert str(round_half_up(Decimal("5"))) == "5.00"


def test_format_money_basic():
    assert format_money(Decimal("1234.5")) == "$1,234.50"


def test_format_money_zero_and_small():
    assert format_money(Decimal("0")) == "$0.00"
    assert format_money(Decimal("0.07")) == "$0.07"


def test_format_money_negative():
    assert format_money(Decimal("-5")) == "-$5.00"


def test_format_money_symbol():
    assert format_money(Decimal("10"), symbol="EUR ") == "EUR 10.00"


def test_split_evenly_exact():
    assert split_evenly(Decimal("10.00"), 2) == [Decimal("5.00"), Decimal("5.00")]


def test_split_evenly_distributes_extra_cents_first():
    shares = split_evenly(Decimal("10.00"), 3)
    assert shares == [Decimal("3.34"), Decimal("3.33"), Decimal("3.33")]
    assert sum(shares) == Decimal("10.00")


def test_split_evenly_single_part():
    assert split_evenly(Decimal("9.99"), 1) == [Decimal("9.99")]


def test_split_evenly_rejects_zero_parts():
    with pytest.raises(ValueError):
        split_evenly(Decimal("1.00"), 0)
