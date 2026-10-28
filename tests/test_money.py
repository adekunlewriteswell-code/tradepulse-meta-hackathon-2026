from decimal import Decimal, localcontext

import pytest

from app.domain.money import MAX_MINOR_UNITS, from_minor_units, to_minor_units


@pytest.mark.parametrize("value, expected", [("0.00", 0), ("0.10", 10), ("19.99", 1999), ("92233720368547758.07", MAX_MINOR_UNITS)])
def test_money_exact_round_trip(value, expected):
    assert to_minor_units(value) == expected
    assert to_minor_units(Decimal(value)) == expected
    assert from_minor_units(expected) == Decimal(value)


@pytest.mark.parametrize("value", [0.1, 1, True])
def test_money_rejects_float_and_implicit_coercion(value):
    with pytest.raises(TypeError):
        to_minor_units(value)


@pytest.mark.parametrize("value", ["0.001", "-0.01", "NaN", "Infinity", "garbage", "92233720368547758.08"])
def test_money_rejects_fractional_minor_units_nonfinite_negative_overflow(value):
    with pytest.raises(ValueError):
        to_minor_units(value)


def test_money_does_not_depend_on_ambient_decimal_precision():
    with localcontext() as context:
        context.prec = 2
        assert to_minor_units("12345.67") == 1234567
        assert from_minor_units(1234567) == Decimal("12345.67")


def test_decimal_calculation_example_is_exact():
    amount = from_minor_units(10) + from_minor_units(20)
    assert amount == Decimal("0.30")
    assert to_minor_units(amount) == 30
