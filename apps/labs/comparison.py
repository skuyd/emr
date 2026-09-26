"""Report columns and conservative, explained comparability using reviewed rules."""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from hashlib import sha256
import re
import unicodedata

from .dictionary import DictionaryError, dictionary_for_version, rules_for_version, normalize_indicator_alias
from .extraction import _unit_key
from .models import CapabilityLevel, ResultType
from .readmodels import checked_reference, effective_rows, reconciliation_rows
from .numerics import calculate_numeric
from .change_metrics import changes_for_cells
from .validation import TREND_BLOCKING_ISSUES, issue, numeric_value, parse_reference_range, validate_observation
from .comparison_policy import abnormal_result, cell_review_required, display_identity, display_category, missing_method_rule, SPECIMEN_LABELS


@dataclass(frozen=True)
class ComparisonColumn:
    document: object
    observation_date: object
    date_label: str
    institution: str = '医院未识别'
    report_label: str = ''
    documents: tuple = ()


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
    plot_eligible: bool = False
    abnormal: object = None
    known_unit: bool = False
    method_rule: object = None
    review_required: bool = False
    sources: tuple = ()
    reference_difference: bool = False
    source_cells: tuple = ()
    catalog: object = None

    @property
    def display_value(self):
        return self.catalog.value.display_value if self.catalog else self.observation.raw_value

    @property
    def standard_reference(self):
        return self.catalog.reference.label if self.catalog and self.catalog.reference else ''

    @property
    def specimen_label(self):
        specimen = self.observation.specimen.strip().upper()
        return SPECIMEN_LABELS.get(specimen, specimen) if specimen not in {'', 'UNKNOWN', 'UNSPECIFIED'} else '标本待确认'

    @property
    def identity_review_required(self):
        return any(item['code'] in {'mapping_unknown', 'specimen_unknown', 'specimen_conflict', 'association_conflict',
                                   'normalization_uncertain', 'recognition_uncertain', 'reported_error', 'revision_conflict'}
                   and (not item.get('fields') or {'raw_name', 'standard_code', 'specimen'}.intersection(item['fields']))
                   for item in self.quality_issues)

    @property
    def latest_sampled_at(self):
        times = [identity.sampled_at for row in (self.sources or (self.observation,))
                 if (identity := getattr(row, 'report_identity', None)) is not None and identity.sampled_at is not None]
        return max(times) if times else None

    @property
    def source_review_labels(self):
        sources = self.sources or (self.observation,)
        codes = {item['code'] for cell in (self.source_cells or (self,)) for item in cell.quality_issues}
        report_reasons = {source.report_identity.reason for source in sources}
        labels = list(dict.fromkeys(source.report_identity.reason_label for source in sources
                                   if source.report_identity.reason_label))
        if (any(getattr(source, 'report_conflict', False) for source in sources)
                or 'report_identity_conflict' in codes and not report_reasons.intersection(
                    {'report_identity_conflict', 'report_revision_conflict'})):
            labels.append('报告归属存在冲突，请核对原件。')
        if 'reported_error' in codes or any(source.reported_error for source in sources):
            labels.append('已标记识别有误')
        if 'revision_conflict' in codes or any(source.revision_conflict for source in sources):
            labels.append('修订冲突待核对')
        if not labels and any(source.review_state in {'AUTOMATIC', 'DEFER'} for source in sources):
            labels.append('待核对')
        return tuple(labels)


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
    shared_unit: str = ''
    specimen_label: str = ''
    multiple_series: bool = False
    reference_ranges: tuple = ()
    trend_links: tuple = ()
    selection_key: str = ''


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
    categories: tuple = ()
    pending_sources: tuple = ()
    selection_groups: tuple = ()

    @property
    def report_count(self):
        return len({getattr(source, 'report_group_key', str(source.parsing_version.document_id))
                    for row in self.rows for entries in row.cells for cell in entries
                    for source in (cell.sources or (cell.observation,))})

    @property
    def image_count(self):
        return len({source.document_page_id for row in self.rows for entries in row.cells for cell in entries
                    for source in (cell.sources or (cell.observation,))})

    @property
    def result_count(self):
        return sum(len(entries) for row in self.rows for entries in row.cells)


def _reference_display_key(value):
    text = unicodedata.normalize('NFKC', value)
    # Printed double dashes separate nonnegative bounds. Do not collapse signs
    # in negative ranges or repair text that contains other OCR fragments.
    pair = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[-—–]{2}\s*(\d+(?:\.\d+)?)\s*', text)
    parsed = parse_reference_range('~'.join(pair.groups()) if pair else text)
    if parsed['kind'] == 'numeric':
        return ('numeric', numeric_value(parsed['low']), numeric_value(parsed['high']),
                parsed['low_inclusive'], parsed['high_inclusive'])
    return ('text', value)


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
    from .catalog_projection import catalog_issues, project_catalog
    catalog = project_catalog(observation)
    issues = catalog_issues(catalog, issues)
    unit = observation.raw_unit
    value = numeric_value(observation.raw_value) if observation.result_type == ResultType.NUMERIC else None
    known_unit = definition is not None and bool(unit) and _unit_key(unit) in {_unit_key(item) for item in definition.unit_forms}
    if catalog:
        unit, value, known_unit = catalog.value.unit, catalog.value.value, catalog.value.reliable
    trustworthy = not ({item["code"] for item in issues} & TREND_BLOCKING_ISSUES)
    method_rule = missing_method_rule(observation, rules)
    comparable = bool(trustworthy and definition and observation.specimen.strip()
                      and observation.specimen.upper() not in {"UNSPECIFIED", "UNKNOWN"}
                      and (observation.method_raw.strip() or method_rule) and known_unit)
    rule = None
    if comparable and value is not None:
        conversions = [item for item in rules if item.get("kind") == "conversion"
                 and all(item.get(field) for field in ("id", "version", "reviewed_by", "rationale", "evidence"))
                 and item.get("code") == observation.standard_code and item.get("specimen") == observation.specimen
                 and item.get("method") == observation.method_raw and _unit_key(item.get("source_unit", "")) == _unit_key(observation.raw_unit if catalog else unit)
                 and _unit_key(item.get("target_unit", "")) in {_unit_key(form) for form in definition.unit_forms}]
        if len(conversions) == 1:
            candidate = conversions[0]
            factor = numeric_value(candidate.get('factor'))
            source_value = numeric_value(observation.raw_value) if catalog else value
            converted = calculate_numeric(lambda: source_value * factor) if source_value is not None and factor is not None and factor > 0 else None
            if converted is None:
                comparable = False
                issues = (*issues, issue('numeric_unsupported', ['raw_value'], rule_version=candidate['version'], rule_id=candidate['id']))
            elif catalog and (converted != catalog.value.value or _unit_key(candidate['target_unit']) != _unit_key(catalog.indicator.unit)):
                comparable = False
                issues = (*issues, issue('normalization_uncertain', ['raw_value', 'raw_unit'],
                                          details='既有换算规则与固定目录的量纲换算不一致，请核对。'))
            else:
                rule, value, unit = candidate, converted, candidate['target_unit']
                if catalog:
                    unit = catalog.indicator.unit
        elif len(conversions) > 1:
            comparable = False
    if catalog and catalog.value.converted and rule is None and comparable:
        rule = {'id': 'catalog-dimension-conversion', 'version': '2026-09-22',
                'source_unit': observation.raw_unit, 'target_unit': unit}
    state = "converted" if comparable and rule else "direct" if comparable else "insufficient"
    method_basis = ('rule:' + method_rule['id'] + ':' + method_rule['version']) if method_rule else observation.method_raw
    key = (catalog.indicator.code if catalog else observation.standard_code, observation.specimen, _unit_key(unit), method_basis, "trusted" if comparable else "insufficient")
    if catalog is None:
        key += (observation.raw_name.strip().casefold(),)
    reference = catalog.comparison(issues) if catalog else checked_reference(observation, issues, dictionary=dictionary, rules=rules)
    plot_trustworthy = not ({item['code'] for item in issues} & TREND_BLOCKING_ISSUES)
    unit_reliable = known_unit and not any(item['code'] in TREND_BLOCKING_ISSUES and 'raw_unit' in item['fields'] for item in issues)
    return ComparisonCell(observation, state, {"direct": "可直接比较", "converted": "经规则换算", "insufficient": "依据不足"}[state],
                          bool(comparable and value is not None and observation.observation_date and observation.capability_level == CapabilityLevel.STABLE),
                          value, unit, rule, issues, reference["label"], key,
                          change_threshold_percent=50 if definition and definition.category == 'TUMOR_MARKER' else 30,
                          plot_eligible=bool(plot_trustworthy and known_unit and value is not None and observation.observation_date),
                          abnormal=catalog.abnormal(issues) if catalog else abnormal_result(observation, issues, reference),
                          known_unit=unit_reliable, method_rule=method_rule, catalog=catalog,
                          review_required=cell_review_required(issues) or bool(catalog and not catalog.value.reliable))


def comparison_view(patient, *, start=None, end=None, category="", categories=(), project="", ordering_profile=None,
                    indicators=None, catalog_order=False):
    from apps.cancer_ordering.profiles import prioritize, prioritize_groups
    from apps.cancer_ordering.readmodels import resolve_ordering

    profile = None if catalog_order else ordering_profile if ordering_profile is not None else resolve_ordering(patient)['profile']
    from .report_reads import assign_report_groups
    from .consolidation import fold_cells, institution_key, latest_daily_cells
    all_sources = effective_rows(patient, include_uncertain=True, include_invalid=True)
    pending_sources = tuple(row for row in all_sources if row.report_identity.status == 'REJECTED')
    all_rows = tuple(row for row in all_sources if row.report_identity.status != 'REJECTED')
    assign_report_groups(patient, all_rows)
    institutions = {str(row.pk): row.comparison_institution for row in all_rows}
    snapshots = {}
    for observation in all_rows:
        version = observation.mapping_dictionary_version
        if version not in snapshots:
            try:
                snapshots[version] = (dictionary_for_version(version), rules_for_version(version))
            except DictionaryError:
                snapshots[version] = (None, ())
    definitions = {version: {item.code: item for item in dictionary.indicators} if dictionary else {}
                   for version, (dictionary, rules) in snapshots.items()}
    all_cells = tuple(comparable_cell(observation, previous=all_rows,
                                     dictionary=snapshots[observation.mapping_dictionary_version][0],
                                     rules=snapshots[observation.mapping_dictionary_version][1]) for observation in all_rows)
    latest, disputed = latest_daily_cells(all_cells)
    latest_ids = {str(cell.observation.pk) for cell in latest}
    changes = changes_for_cells(tuple(replace(cell, trend_eligible=False, plot_eligible=False)
                                     if str(cell.observation.pk) not in latest_ids else cell for cell in all_cells))
    display_names = {str(cell.observation.pk): display_identity(cell.observation,
                  definitions[cell.observation.mapping_dictionary_version].get(cell.observation.standard_code), cell.quality_issues,
                  dictionary=snapshots[cell.observation.mapping_dictionary_version][0]) for cell in all_cells}
    from .catalog import load_catalog
    catalog = load_catalog()
    display_names = {key: entry.name if (entry := catalog.match(name)) else name
                     for key, name in display_names.items()}
    catalogs = {str(cell.observation.pk): cell.catalog for cell in all_cells}
    display_names.update({key: projection.indicator.name for key, projection in catalogs.items() if projection})
    identities = {key: normalize_indicator_alias(name) for key, name in display_names.items()}
    project = project.strip().casefold()
    matched = {identities[str(row.pk)] for row in all_rows if not project or project in
               ' '.join((row.standard_code, row.standard_name, row.raw_name, display_names[str(row.pk)])).casefold()}
    selected_categories = {display_category(item) for item in (categories or ((category,) if category else ())) if item}
    available_categories = set() if catalog_order else set(selected_categories)
    # Explicit category aliases, never the latest record's arbitrary category.
    identity_categories = defaultdict(set)
    identity_entries = defaultdict(dict)
    identity_names = {}
    for row in all_rows:
        identity = identities[str(row.pk)]
        projection = catalogs[str(row.pk)]
        identity_categories[identity].add(projection.indicator.category if projection else 'OTHER')
        identity_names.setdefault(identity, display_names[str(row.pk)])
        if projection:
            identity_entries[identity][projection.indicator.category] = projection.indicator
    category_positions = {name: position for position, name in enumerate(catalog.groups)}
    category_positions['OTHER'] = len(category_positions)
    def category_position(name):
        return category_positions.get(name, len(category_positions))
    category_by_identity = {key: ' / '.join(sorted(values, key=category_position if catalog_order else None))
                            for key, values in identity_categories.items()}
    # Keys represent the existing complete history, independent of date/search
    # filters and of which catalog category contains a repeated indicator.
    selection_keys = {}
    for identity, entries in identity_entries.items():
        codes = {entry.code for entry in entries.values()}
        if len(codes) == 1:
            selection_keys[identity] = 'indicator:' + next(iter(codes))
    for identity in identity_categories:
        selection_keys.setdefault(identity, 'history:' + sha256(identity.encode()).hexdigest())
    chosen = set(indicators) if indicators is not None else {
        selection_keys[key] for key, values in identity_categories.items()
        if not selected_categories or selected_categories.intersection(values)}
    def indicator_position(identity, group=None):
        entries = identity_entries[identity]
        relevant = [entry for category, entry in entries.items() if group is None or category == group]
        return min((min(entry.source_rows) for entry in relevant), default=float('inf')), identity
    from .presentation import CATEGORY_LABELS
    selection_groups = tuple({'category': group, 'label': CATEGORY_LABELS.get(group, group),
        'indicators': tuple({'key': selection_keys[key], 'label': identity_names[key], 'selected': selection_keys[key] in chosen}
            for key in sorted((key for key, values in identity_categories.items() if group in values),
                              key=lambda key: indicator_position(key, group)))}
        for group in sorted({group for values in identity_categories.values() for group in values}, key=category_position))
    selected = []
    for cell in all_cells:
        observation = cell.observation
        identity = identities[str(observation.pk)]
        group = category_by_identity[identity]
        available_categories.update(identity_categories[identity])
        if start and (not observation.observation_date or observation.observation_date < start):
            continue
        if end and (not observation.observation_date or observation.observation_date > end):
            continue
        if identity not in matched:
            continue
        if selection_keys[identity] not in chosen:
            continue
        selected.append((replace(cell, change=changes[str(observation.pk)]), group))
    def column_key(observation):
        identity = observation.report_identity
        uncertain_date = identity.reason == 'sampling_datetime_conflict' and len(identity.sampling_dates) != 1
        day = None if uncertain_date else observation.observation_date
        source = str(observation.parsing_version.document_id) if day is None else ''
        return (institution_key(observation), day, source)
    # A report can contain corrected dates; keep those explicit rather than silently choosing one.
    column_map = defaultdict(dict)
    column_institutions = {}
    for cell, _ in selected:
        row = cell.observation
        key = column_key(row)
        column_map[key][row.parsing_version.document_id] = row.parsing_version.document
        column_institutions[key] = institutions[str(row.pk)]
    column_keys = sorted(column_map, key=lambda key: (key[1] is None, key[1] or date.max, str(key[0]), key[2]))
    columns = tuple(ComparisonColumn(next(iter(column_map[key].values())), key[1],
        key[1].isoformat() if key[1] else '日期待核对', column_institutions[key],
        documents=tuple(column_map[key].values())) for key in column_keys)
    grouped = defaultdict(lambda: defaultdict(list))
    labels = {}
    for cell, category_label in selected:
        observation = cell.observation
        identity = identities[str(observation.pk)]
        grouped[identity][column_key(observation)].append(cell)
        labels.setdefault(identity, (display_names[str(observation.pk)], category_label))
    from .trends import TrendPoint, _positioned, _line_segments, _blocked_dates
    rows = []
    for key, values in sorted(grouped.items()):
        entries = tuple(fold_cells(values.get(column, ())) for column in column_keys)
        row_cells = tuple(cell for group in entries for cell in group)
        daily, disputed = latest_daily_cells(row_cells)
        plotted = tuple(cell for cell in daily if cell.plot_eligible)
        series_keys = {(cell.group_key, institution_key(cell.observation), cell.trend_eligible) for cell in plotted}
        points = tuple(TrendPoint(cell.observation, cell.numeric_value, change=cell.change) for cell in plotted)
        sparkline = _positioned(points) if points else ()
        multiple_series = len(series_keys) > 1
        blockers = (*disputed, *(cell for cell in daily if not cell.trend_eligible))
        segments = (_line_segments(sparkline, blocked_dates=_blocked_dates(blockers, plotted[0]))
                    if plotted and not multiple_series and all(cell.trend_eligible for cell in plotted) else ())
        units = {cell.unit for cell in row_cells if cell.known_unit}
        shared_unit = row_cells[0].unit if len(units) == 1 and all(cell.known_unit for cell in row_cells) else ''
        reference_groups = {}
        different_units = not shared_unit and len({cell.observation.raw_unit.strip() for cell in row_cells}) > 1
        for column, cells in zip(columns, entries):
            for cell in cells:
                for source in cell.sources or (cell.observation,):
                    if cell.catalog:
                        continue
                    value = source.reference_range_raw.strip()
                    if not value:
                        continue
                    unit = (source.raw_unit.strip() or '单位未提供') if different_units and value else ''
                    reference = reference_groups.setdefault((_reference_display_key(value), unit),
                        {'value': value or '未提供', 'unit': unit, 'dates': []})
                    label = ' '.join(filter(None, (column.date_label, column.report_label)))
                    if label not in reference['dates']:
                        reference['dates'].append(label)
        reference_ranges = tuple({**reference, 'dates': tuple(reference['dates'])}
                                 for reference in reference_groups.values())
        trend_codes = {}
        for cell in plotted:
            trend_code = cell.catalog.indicator.code if cell.catalog else cell.observation.standard_code
            trend_codes.setdefault(trend_code, {'code': trend_code,
                'label': cell.specimen_label + '趋势'})
        known_codes = sorted({cell.observation.standard_code for cell in row_cells
                              if cell.observation.standard_code in definitions[cell.observation.mapping_dictionary_version]})
        catalog_codes = sorted({cell.catalog.indicator.code for cell in row_cells if cell.catalog})
        code = next(iter(trend_codes), catalog_codes[0] if catalog_codes else known_codes[0] if known_codes else row_cells[0].observation.standard_code)
        rows.append(ComparisonRow(code, labels[key][0], labels[key][1],
                                  '', entries, () if multiple_series else sparkline, segments,
                                  len({point.observation.observation_date for point in sparkline}) >= 2,
                                  shared_unit, '', multiple_series, reference_ranges, tuple(trend_codes.values()), selection_keys[key]))
    if catalog_order:
        rows.sort(key=lambda row: (min(category_position(group) for group in identity_categories[normalize_indicator_alias(row.standard_name)]),
                                  indicator_position(normalize_indicator_alias(row.standard_name))))
    rows = tuple(rows)
    category_rows = defaultdict(list)
    for row in rows:
        category_rows[row.category].append(row)
    versions = {observation.parsing_version_id: observation.parsing_version for observation in all_rows}
    # Include an empty current parse, which otherwise could hide all previous human edits.
    from apps.processing.models import ParsingVersion
    for version in ParsingVersion.objects.filter(active=True, document__patient=patient, document__deleted_at__isnull=True):
        versions[version.pk] = version
    reconciliation = tuple(item for version in versions.values() for item in reconciliation_rows(version, [row for row in all_sources if row.parsing_version_id == version.pk]))
    grouped_rows = category_rows.items() if catalog_order else sorted(category_rows.items())
    groups = tuple(ComparisonGroup(key, tuple(value)) for key, value in grouped_rows)
    options = tuple((code, CATEGORY_LABELS.get(code, code)) for code in sorted(available_categories, key=category_position if catalog_order else None))
    return ComparisonView(columns, rows if catalog_order else prioritize(rows, profile), reconciliation,
        groups if catalog_order else prioritize_groups(groups, profile), options, pending_sources, selection_groups)
