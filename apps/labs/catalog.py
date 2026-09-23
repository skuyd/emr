"""Fixed, source-traceable indicators and patient-specific reference selection.

This module never edits observations or grants calculation eligibility. Callers
must retain the source/review gates when using a standardized value.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cached_property, lru_cache
import json
from pathlib import Path
import re
import unicodedata

from .dictionary import normalize_indicator_alias
from .numerics import NUMBER, calculate_numeric, numeric_value


PHASES = ('卵泡期', '排卵期', '黄体期', '绝经期')


def age_on(birth_date, sampled_on):
    if not isinstance(birth_date, date) or not isinstance(sampled_on, date) or sampled_on < birth_date:
        return None
    return sampled_on.year - birth_date.year - ((sampled_on.month, sampled_on.day) < (birth_date.month, birth_date.day))


@dataclass(frozen=True)
class Reference:
    label: str
    low: Decimal | None = None
    high: Decimal | None = None
    low_inclusive: bool = True
    high_inclusive: bool = True
    qualitative: str = ''
    condition: str = ''

    def compare(self, raw):
        if self.qualitative:
            value = str(raw).strip()
            if self.qualitative == '阴性':
                return 'within' if value in {'阴性', 'negative', '-'} else 'different' if value in {'阳性', '弱阳性', 'positive', '+', '++', '+++', '++++'} else 'unavailable'
            return 'within' if value == self.qualitative else 'unavailable'
        value = numeric_value(str(raw))
        if value is None:
            bounded = re.fullmatch(rf'([<>]=?|≤|≥)\s*({NUMBER})', unicodedata.normalize('NFKC', str(raw)))
            if bounded:
                operator, bound = bounded.groups()
                value = numeric_value(bound)
                if value is None:
                    return 'unavailable'
                inclusive = '=' in operator or operator in {'≤', '≥'}
                if operator[0] in {'<', '≤'} and self.low is not None and (value < self.low or value == self.low and not inclusive):
                    return 'below'
                if operator[0] in {'>', '≥'} and self.high is not None and (value > self.high or value == self.high and not inclusive):
                    return 'above'
            return 'unavailable'
        if self.low is not None and (value < self.low or value == self.low and not self.low_inclusive):
            return 'below'
        if self.high is not None and (value > self.high or value == self.high and not self.high_inclusive):
            return 'above'
        return 'within'


def _reference(row):
    low, high = row['low'], row['high']
    if low is None and high is None:
        return None
    lower, upper = numeric_value(low), numeric_value(high)
    if lower is not None and upper is not None:
        return Reference(f'{low}–{high}', lower, upper, condition=row['condition'])
    text = unicodedata.normalize('NFKC', str(high or low or ''))
    match = re.fullmatch(r'([<>]=?|≤|≥)\s*([0-9.]+)', text)
    if match:
        operator, bound = match.groups()
        value = numeric_value(bound)
        if value is None:
            return None
        greater = operator[0] in {'>', '≥'}
        inclusive = '=' in operator or operator in {'≤', '≥'}
        return Reference(text, value if greater else None, None if greater else value,
                         inclusive, inclusive, condition=row['condition'])
    if lower is None and upper is None:
        return Reference(text, qualitative=text, condition=row['condition'])
    return None


def _matches_condition(row, sex, age, phase):
    condition = unicodedata.normalize('NFKC', row['condition']).replace('～', '-').replace('~', '-')
    if condition in PHASES:
        return sex == 'F' and phase == condition
    if '男' in condition and sex != 'M' or '女' in condition and sex != 'F':
        return False
    # Explicit source corrections from spec §3, tied to their source rows.
    if row['row'] == 47:
        return sex == 'F' and age is not None and age <= 50
    if row['row'] == 48:
        return sex == 'F' and age is not None and age > 50
    bounds = re.search(r'(\d+)\s*-\s*(\d+)', condition)
    if bounds:
        low, high = map(int, bounds.groups())
        if row['row'] == 126:
            low = 16
        if row['row'] == 163:
            low = 6
        return age is not None and low <= age <= high
    return True


def unit_key(value):
    # Prefix case is significant: mIU is not MIU. Superscripts retain exponents.
    value = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+', lambda m: '^' + unicodedata.normalize('NFKC', m[0]), str(value))
    value = re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).replace('μ', 'u').replace('µ', 'u')
    value = re.sub(r'/(m?l)$', lambda match: '/mL' if match[1] == 'ml' else '/L', value)
    value = value.replace('cells/', '/').replace('个/', '/')
    return {'mIu/L': 'mIU/L', 'IU/ml': 'IU/mL', 'pg/ml': 'pg/mL', 'ml/min': 'mL/min', '秒': 's'}.get(value, value)


def _unit_basis(unit):
    unit = unit_key(unit)
    if unit in {'1', 'L/L'}:
        return 'ratio', Decimal(1)
    if unit == '%':
        return 'ratio', Decimal('0.01')
    count = re.fullmatch(r'(?:10\^([+-]?\d+))?/(L|mL|uL)', unit)
    volumes = {'L': Decimal(1), 'dL': Decimal('0.1'), 'mL': Decimal('0.001'), 'uL': Decimal('0.000001')}
    if count:
        exponent = int(count[1] or 0)
        if abs(exponent) > 15:
            return None
        return 'count/volume', Decimal(10) ** exponent / volumes[count[2]]
    concentration = re.fullmatch(r'(kg|g|mg|ug|ng|pg|mol|mmol|umol|nmol|pmol|IU|mIU|kIU|U|mU|kU)/(L|dL|mL|uL)', unit)
    if concentration:
        numerator, denominator = concentration.groups()
        for base in ('mol', 'IU', 'U', 'g'):
            if numerator.endswith(base):
                prefix = numerator[:-len(base)]
                factors = {'': '1', 'k': '1000', 'm': '0.001', 'u': '0.000001', 'n': '0.000000001', 'p': '0.000000000001'}
                return base + '/volume', Decimal(factors[prefix]) / volumes[denominator]
    return None


@dataclass(frozen=True)
class StandardValue:
    display_value: str
    unit: str
    value: Decimal | None
    reliable: bool
    converted: bool = False


@dataclass(frozen=True)
class Indicator:
    code: str
    name: str
    category: str
    specimen: str
    unit: str
    aliases: tuple
    source_rows: tuple
    ranges: tuple

    def reference_for(self, *, sex='', birth_date=None, sampled_on=None, phase=''):
        age = age_on(birth_date, sampled_on)
        matches = [_reference(row) for row in self.ranges if _matches_condition(row, sex, age, phase)]
        matches = [item for item in matches if item is not None]
        return matches[0] if len(matches) == 1 else None

    @property
    def phase_references(self):
        return tuple(item for row in self.ranges if row['condition'] in PHASES and (item := _reference(row)))

    def standardize(self, raw_value, raw_unit):
        value = numeric_value(str(raw_value))
        bounded = re.fullmatch(rf'([<>]=?|≤|≥)\s*({NUMBER})', unicodedata.normalize('NFKC', str(raw_value)))
        operand = numeric_value(bounded[2]) if bounded else value
        source, target = unit_key(raw_unit), unit_key(self.unit)
        if source == target:
            return StandardValue(str(raw_value), self.unit, value, True)
        source_basis, target_basis = _unit_basis(source), _unit_basis(target)
        if source_basis and target_basis and source_basis[0] == target_basis[0] and operand is not None:
            converted = calculate_numeric(lambda: operand * source_basis[1] / target_basis[1])
            if converted is not None:
                label = format(converted, 'f')
                if '.' in label:
                    label = label.rstrip('0').rstrip('.')
                return StandardValue((bounded[1] if bounded else '') + label, self.unit,
                                     None if bounded else converted, True, True)
        return StandardValue(str(raw_value), str(raw_unit), None, False)


@dataclass(frozen=True)
class Catalog:
    source_sha256: str
    source_rows: tuple
    groups: tuple
    indicators: tuple[Indicator, ...]

    @cached_property
    def _aliases(self):
        index = {}
        for item in self.indicators:
            for key in {normalize_indicator_alias(alias) for alias in (item.name, *item.aliases)}:
                index.setdefault(key, []).append(item)
        return index

    def candidates(self, name):
        return list(self._aliases.get(normalize_indicator_alias(name), ()))

    def match(self, name, *, specimen='', panel=''):
        candidates = self.candidates(name)
        specimen = {'尿液': 'URINE', '尿': 'URINE', '粪便': 'STOOL', '大便': 'STOOL',
                    '血液': 'BLOOD', '血清': 'BLOOD', '血浆': 'BLOOD'}.get(specimen, specimen)
        if specimen and specimen not in {'UNSPECIFIED', 'UNKNOWN'}:
            candidates = [item for item in candidates if item.specimen == specimen]
        if panel and len(candidates) > 1:
            panel = {'CBC': '血常规（急诊）', 'HEMATOLOGY': '血常规（急诊）',
                     'INFLAMMATION': '炎症三项'}.get(panel, panel)
            hint = 'URINE' if '尿' in panel or panel == 'URINALYSIS' else 'STOOL' if '大便' in panel or '粪便' in panel or panel == 'STOOL' else ''
            candidates = [item for item in candidates if item.specimen == hint] if hint else [item for item in candidates if item.category == panel]
        return candidates[0] if len(candidates) == 1 else None


@lru_cache(maxsize=1)
def load_catalog():
    payload = json.loads((Path(__file__).parent / 'dictionaries/indicator-catalog.json').read_text(encoding='utf-8'))
    return Catalog(payload['source_sha256'], tuple(payload['source_rows']), tuple(payload['groups']),
                   tuple(Indicator(**{**item, 'aliases': tuple(item['aliases']),
                                      'source_rows': tuple(item['source_rows']), 'ranges': tuple(item['ranges'])})
                         for item in payload['indicators']))
