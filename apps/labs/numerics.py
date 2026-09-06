"""Bound computational representations without imposing clinical value limits."""

from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
import re
import unicodedata


NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
# Deliberately wider than report values; raw text outside these implementation
# limits remains available, but never participates in derived calculations.
MAX_DIGITS = 128
MAX_EXPONENT = 1000
CALCULATION_CONTEXT = Context(prec=MAX_DIGITS, Emin=-10000, Emax=10000, rounding=ROUND_HALF_EVEN)


def supported_number(value):
    return (value.is_finite() and len(value.as_tuple().digits) <= MAX_DIGITS
            and abs(value.as_tuple().exponent) <= MAX_EXPONENT
            and abs(value.adjusted()) <= MAX_EXPONENT)


def numeric_value(value):
    text = unicodedata.normalize('NFKC', str(value if value is not None else '')).strip().replace('−', '-')
    if len(text) > MAX_DIGITS + 32 or re.fullmatch(NUMBER, text) is None:
        return None
    try:
        result = Decimal(text)
        return result if supported_number(result) else None
    except DecimalException:
        return None


def calculate_numeric(operation):
    try:
        with localcontext(CALCULATION_CONTEXT):
            result = operation()
        return result if supported_number(result) else None
    except (DecimalException, ZeroDivisionError, OverflowError):
        return None
