"""Exact two-decimal currency representation; no float conversion or rounding."""

from decimal import Decimal, InvalidOperation

MAX_MINOR_UNITS = 2**63 - 1
MIN_MINOR_UNITS = -(2**63)


def to_minor_units(amount: Decimal | str, *, signed: bool = False) -> int:
    if not isinstance(amount, (Decimal, str)):
        raise TypeError("Money requires Decimal or a decimal string; floats are forbidden")
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError("Invalid monetary amount") from exc
    if not value.is_finite() or (value < 0 and not signed):
        raise ValueError("Money must be finite; negative values require signed=True")
    if value.is_zero():
        return 0
    # Work from the exact Decimal tuple. No multiplication, rounding, exponent
    # overflow/underflow, ambient context, or binary floating-point is involved.
    sign, digits, exponent = value.as_tuple()
    shift = exponent + 2
    if shift < 0:
        if -shift >= len(digits) or any(digits[shift:]):
            raise ValueError("Money supports at most two fractional decimal places")
        digits = digits[:shift]
        shift = 0
    if len(digits) + shift > 19:
        raise ValueError("Money exceeds SQLite signed 64-bit integer capacity")
    units = int(''.join(map(str, digits))) * 10**shift
    units = -units if sign else units
    if not (MIN_MINOR_UNITS if signed else 0) <= units <= MAX_MINOR_UNITS:
        raise ValueError("Money exceeds SQLite signed 64-bit integer capacity")
    return units


def from_minor_units(amount: int, *, signed: bool = False) -> Decimal:
    if type(amount) is not int:
        raise TypeError("Minor units must be an integer")
    if not (MIN_MINOR_UNITS if signed else 0) <= amount <= MAX_MINOR_UNITS:
        raise ValueError("Minor units outside storage bounds")
    return Decimal((int(amount < 0), tuple(map(int, str(abs(amount)))), -2))
