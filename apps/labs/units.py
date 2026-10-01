"""Shared display and matching rules that preserve the original OCR fields."""

import re
import unicodedata


_COUNT = re.compile(r'[×xX]?10(?:\^([+-]?\d+)|(\d+))(?:/(L|mL|μL))?')


def normalize_unit_text(value):
    # NFKC alone turns 10⁹ into 109 and loses the exponent boundary.
    value = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+',
                   lambda match: '^' + unicodedata.normalize('NFKC', match[0]), str(value))
    return unicodedata.normalize('NFKC', value)


def normalize_unit(value):
    """Normalize unit spelling only; do not infer a denominator or change values."""
    unit = re.sub(r'\s+', '', normalize_unit_text(value))
    unit = unit.replace('cells/', '/').replace('个/', '/')
    unit = re.sub(r'(^|/)([mMμu]?)[lL](?=/|$)',
                  lambda match: match[1] + {'': '', 'm': 'm', 'M': 'm', 'μ': 'μ', 'u': 'μ'}[match[2]] + 'L', unit)
    unit = re.sub(r'^([muμMU])mo1/(L|mL|μL)$',
                  lambda match: ('m' if match[1] in {'m', 'M'} else 'μ') + 'mol/' + match[2], unit)
    unit = re.sub(r'(^|/)u(?=(?:g|mol|L|IU|U)(?:/|$))', r'\1μ', unit)
    count = _COUNT.fullmatch(unit)
    if count:
        return '10^' + (count[1] or count[2]) + ('/' + count[3] if count[3] else '')
    return {'mIu/L': 'mIU/L', '秒': 's'}.get(unit, unit)


def count_unit_parts(value):
    """Return the count exponent and optional volume, preserving their scale."""
    count = _COUNT.fullmatch(normalize_unit(value))
    return (count[1], count[3] or '') if count else None


def unit_key(value):
    # Prefix case is significant: mIU is not MIU.
    return normalize_unit(value).replace('μ', 'u')
