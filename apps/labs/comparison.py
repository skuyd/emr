"""Report columns and conservative, explained comparability using reviewed rules."""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date

from .dictionary import DictionaryError, dictionary_for_version, rules_for_version
from .extraction import _unit_key
from .models import CapabilityLevel, ResultType
from .readmodels import checked_reference, effective_rows, reconciliation_rows
from .numerics import calculate_numeric
from .change_metrics import changes_for_cells
from .validation import TREND_BLOCKING_ISSUES, issue, numeric_value, validate_observation


@dataclass(frozen=True)
class ComparisonColumn:
    document: object
    observation_date: object
    date_label: str


@dataclass(frozen=True)
class ComparisonCell:
    observation: object
    comparability: str
    comparability_label: str
    trend_eligible: bool
    numeric_value: object
    unit: str
    rule: object
    quality_issues: tuple
    reference_label: str
    group_key: tuple
    change_threshold_percent: int = 30
    change: object = None


@dataclass(frozen=True)
class ComparisonRow:
    standard_code: str
    standard_name: str
    category: str
    basis_label: str
    cells: tuple
    sparkline: tuple = ()
    sparkline_segments: tuple = ()
    sparkline_has_trend: bool = False


@dataclass(frozen=True)
class ComparisonGroup:
    category: str
    rows: tuple

    @property
    def label(self):
        from .presentation import CATEGORY_LABELS
        return CATEGORY_LABELS.get(self.category, self.category)


@dataclass(frozen=True)
class ComparisonView:
    columns: tuple
    rows: tuple
    reconciliation: tuple
    groups: tuple = ()


def comparable_cell(observation, *, previous=(), dictionary=None, rules=None):
    version = getattr(observation, "mapping_dictionary_version", observation.dictionary_version)
    if dictionary is None:
        try:
            dictionary = dictionary_for_version(version)
        except DictionaryError:
            dictionary = None
    if rules is None:
        rules = rules_for_version(version) if dictionary is not None else ()
    definition = next((item for item in dictionary.indicators if item.code == observation.standard_code), None) if dictionary else None
    issues = validate_observation(observation, previous=previous, dictionary=dictionary, rules=rules)
    unit = observation.raw_unit
    value = numeric_value(observation.raw_value) if observation.result_type == ResultType.NUMERIC else None
    known_unit = definition is not None and bool(unit) and _unit_key(unit) in {_unit_key(item) for item in definition.unit_forms}
    trustworthy = not ({item["code"] for item in issues} & TREND_BLOCKING_ISSUES)
    comparable = bool(trustworthy and definition and observation.specimen.strip()
                      and observation.specimen.upper() not in {"UNSPECIFIED", "UNKNOWN"}
                      and observation.method_raw.strip() and known_unit)
    rule = None
    if comparable and value is not None:
        conversions = [item for item in rules if item.get("kind") == "conversion"
                 and all(item.get(field) for field in ("id", "version", "reviewed_by", "rationale", "evidence"))
                 and item.get("code") == observation.standard_code and item.get("specimen") == observation.specimen
                 and item.get("method") == observation.method_raw and _unit_key(item.get("source_unit", "")) == _unit_key(unit)
                 and _unit_key(item.get("target_unit", "")) in {_unit_key(form) for form in definition.unit_forms}]
        if len(conversions) == 1:
            candidate = conversions[0]
            factor = numeric_value(candidate.get('factor'))
            converted = calculate_numeric(lambda: value * factor) if factor is not None and factor > 0 else None
            if converted is None:
                comparable = False
                issues = (*issues, issue('numeric_unsupported', ['raw_value'], rule_version=candidate['version'], rule_id=candidate['id']))
            else:
                rule, value, unit = candidate, converted, candidate['target_unit']
        elif len(conversions) > 1:
            comparable = False
    state = "converted" if comparable and rule else "direct" if comparable else "insufficient"
    key = (observation.standard_code, observation.specimen, _unit_key(unit), observation.method_raw, "trusted" if comparable else "insufficient")
    reference = checked_reference(observation, issues, dictionary=dictionary, rules=rules)
    return ComparisonCell(observation, state, {"direct": "可直接比较", "converted": "经规则换算", "insufficient": "依据不足"}[state],
                          bool(comparable and value is not None and observation.observation_date and observation.capability_level == CapabilityLevel.STABLE),
                          value, unit, rule, issues, reference["label"], key,
                          change_threshold_percent=50 if definition and definition.category == 'TUMOR_MARKER' else 30)


def comparison_view(patient, *, start=None, end=None, category="", project=""):
    all_rows = effective_rows(patient, include_uncertain=True)
    all_cells = tuple(comparable_cell(observation, previous=all_rows) for observation in all_rows)
    changes = changes_for_cells(all_cells)
    selected = []
    for cell in all_cells:
        observation = cell.observation
        if start and (not observation.observation_date or observation.observation_date < start):
            continue
        if end and (not observation.observation_date or observation.observation_date > end):
            continue
        if project and project.casefold() not in " ".join((observation.standard_code, observation.standard_name, observation.raw_name)).casefold():
            continue
        try:
            definition = next((item for item in dictionary_for_version(observation.mapping_dictionary_version).indicators if item.code == observation.standard_code), None)
        except DictionaryError:
            definition = None
        group = definition.category if definition else "未归类"
        if category and category != group:
            continue
        selected.append((replace(cell, change=changes[str(observation.pk)]), group))
    # A report can contain corrected dates; keep those explicit rather than silently choosing one.
    column_map = {(cell.observation.parsing_version.document_id, cell.observation.observation_date): cell.observation.parsing_version.document for cell, _ in selected}
    column_keys = sorted(column_map, key=lambda key: (key[1] is None, key[1] or date.max, str(key[0])))
    columns = tuple(ComparisonColumn(column_map[key], key[1], key[1].isoformat() if key[1] else "日期未识别") for key in column_keys)
    grouped = defaultdict(lambda: defaultdict(list))
    labels = {}
    for cell, category_label in selected:
        observation = cell.observation
        grouped[cell.group_key][(observation.parsing_version.document_id, observation.observation_date)].append(cell)
        labels[cell.group_key] = (observation.standard_name, category_label)
    from .trends import TrendPoint, _positioned, _line_segments
    rows = []
    for key, values in sorted(grouped.items()):
        entries = tuple(tuple(values.get(column, ())) for column in column_keys)
        points = tuple(TrendPoint(cell.observation, cell.numeric_value, change=cell.change)
                       for group in entries for cell in group if cell.trend_eligible)
        sparkline = _positioned(points) if points else ()
        rows.append(ComparisonRow(key[0], labels[key][0], labels[key][1],
                                  f"{key[1] or '标本未识别'} · {key[2] or '单位未识别'} · {key[3] or '方法未识别'}",
                                  entries, sparkline, _line_segments(sparkline),
                                  len({point.observation.observation_date for point in sparkline}) >= 2))
    rows = tuple(rows)
    category_rows = defaultdict(list)
    for row in rows:
        category_rows[row.category].append(row)
    versions = {observation.parsing_version_id: observation.parsing_version for observation in all_rows}
    # Include an empty current parse, which otherwise could hide all previous human edits.
    from apps.processing.models import ParsingVersion
    for version in ParsingVersion.objects.filter(active=True, document__patient=patient, document__deleted_at__isnull=True):
        versions[version.pk] = version
    reconciliation = tuple(item for version in versions.values() for item in reconciliation_rows(version, [row for row in all_rows if row.parsing_version_id == version.pk]))
    return ComparisonView(columns, rows, reconciliation,
                          tuple(ComparisonGroup(key, tuple(value)) for key, value in sorted(category_rows.items())))
