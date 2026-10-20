from datetime import date, timedelta
from decimal import Decimal

from minicart.discounts import DiscountCode, apply_codes, check_code, discount_amount

TODAY = date(2026, 3, 15)
TEN_PERCENT = DiscountCode("SAVE10", "percent", Decimal("10"))
FIVE_OFF = DiscountCode("FIVE", "fixed", Decimal("5"))


def test_percent_discount():
    assert discount_amount(TEN_PERCENT, Decimal("80.00")) == Decimal("8.00")


def test_percent_discount_rounds_to_cents():
    assert discount_amount(TEN_PERCENT, Decimal("33.33")) == Decimal("3.33")


def test_fixed_discount():
    assert discount_amount(FIVE_OFF, Decimal("20.00")) == Decimal("5.00")


def test_discount_on_an_empty_cart_is_zero():
    assert discount_amount(TEN_PERCENT, Decimal("0")) == Decimal("0.00")
    assert discount_amount(FIVE_OFF, Decimal("0")) == Decimal("0.00")


def test_fixed_discount_never_exceeds_a_small_subtotal_through_apply_codes():
    total, _ = apply_codes([DiscountCode("BIG", "fixed", Decimal("50"))], Decimal("12.00"), TODAY)
    assert total == Decimal("12.00")


def test_valid_code():
    assert check_code(TEN_PERCENT, Decimal("50"), TODAY) == (True, "")


def test_expired_code_is_refused():
    code = DiscountCode("OLD", "percent", Decimal("10"), valid_until=TODAY - timedelta(days=1))
    ok, reason = check_code(code, Decimal("50"), TODAY)
    assert not ok and "expired" in reason


def test_code_valid_in_the_future():
    code = DiscountCode("NEW", "percent", Decimal("10"), valid_until=TODAY + timedelta(days=30))
    assert check_code(code, Decimal("50"), TODAY)[0]


def test_minimum_subtotal_is_enforced():
    code = DiscountCode("BIGSPEND", "percent", Decimal("10"), min_subtotal=Decimal("100"))
    ok, reason = check_code(code, Decimal("99.00"), TODAY)
    assert not ok and "at least" in reason
    assert check_code(code, Decimal("150.00"), TODAY)[0]


def test_no_codes_means_no_discount():
    assert apply_codes([], Decimal("100"), TODAY) == (Decimal("0.00"), [])


def test_single_code_applies():
    total, notes = apply_codes([TEN_PERCENT], Decimal("100"), TODAY)
    assert total == Decimal("10.00") and notes == []


def test_invalid_code_is_reported_and_ignored():
    code = DiscountCode("OLD", "percent", Decimal("10"), valid_until=TODAY - timedelta(days=5))
    total, notes = apply_codes([code], Decimal("100"), TODAY)
    assert total == Decimal("0.00") and len(notes) == 1


def test_stackable_codes_combine_on_the_remaining_amount():
    a = DiscountCode("A", "percent", Decimal("10"), stackable=True)
    b = DiscountCode("B", "percent", Decimal("10"), stackable=True)
    total, notes = apply_codes([a, b], Decimal("100"), TODAY)
    assert total == Decimal("19.00") and notes == []  # 10 off 100, then 10% of the remaining 90


def test_two_non_stackable_codes_only_the_best_applies():
    small = DiscountCode("SMALL", "percent", Decimal("5"))
    big = DiscountCode("BIG", "percent", Decimal("20"))
    total, notes = apply_codes([small, big], Decimal("100"), TODAY)
    assert total == Decimal("20.00")
    assert any("SMALL" in n for n in notes)


def test_total_discount_never_exceeds_the_subtotal():
    a = DiscountCode("A", "fixed", Decimal("30"), stackable=True)
    b = DiscountCode("B", "fixed", Decimal("30"), stackable=True)
    total, _ = apply_codes([a, b], Decimal("40"), TODAY)
    assert total == Decimal("40.00")
