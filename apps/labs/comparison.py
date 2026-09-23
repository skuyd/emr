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
    def latest_sampled_at(self):
        times = [identity.sampled_at for row in (self.sources or (self.observation,))
                 if (identity := getattr(row, 'report_identity', None)) is not None and identity.sampled_at is not None]
        return max(times) if times else None


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


def comparison_view(patient, *, start=None, end=None, category="", categories=(), project="", ordering_profile=None):
    from apps.cancer_ordering.profiles import prioritize, prioritize_groups
    from apps.cancer_ordering.readmodels import resolve_ordering

    profile = ordering_profile if ordering_profile is not None else resolve_ordering(patient)['profile']
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
    identities = {str(cell.observation.pk): display_identity(cell.observation,
                  definitions[cell.observation.mapping_dictionary_version].get(cell.observation.standard_code), cell.quality_issues) for cell in all_cells}
    catalogs = {str(cell.observation.pk): cell.catalog for cell in all_cells}
    identities.update({key: (projection.indicator.code, projection.indicator.specimen, '')
                       for key, projection in catalogs.items() if projection})
    for cell in all_cells:
        if cell.catalog is None:
            key = str(cell.observation.pk)
            code, specimen, unresolved = identities[key]
            identities[key] = (code, specimen, unresolved or 'other:' + cell.observation.raw_name.strip().casefold())
    project = project.strip().casefold()
    matched = {identities[str(row.pk)] for row in all_rows if not project or project in ' '.join((row.standard_code, row.standard_name, row.raw_name)).casefold()}
    selected_categories = {display_category(item) for item in (categories or ((category,) if category else ())) if item}
    available_categories = set(selected_categories)
    # Explicit category aliases, never the latest record's arbitrary category.
    identity_categories = defaultdict(set)
    for row in all_rows:
        definition = definitions[row.mapping_dictionary_version].get(row.standard_code)
        catalog = catalogs[str(row.pk)]
        identity_categories[identities[str(row.pk)]].add(catalog.indicator.category if catalog else 'OTHER')
    category_by_identity = {key: ' / '.join(sorted(values)) for key, values in identity_categories.items()}
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
        if selected_categories and not selected_categories.intersection(identity_categories[identity]):
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
        definition = definitions[observation.mapping_dictionary_version].get(observation.standard_code)
        labels[identity] = (cell.catalog.indicator.name if cell.catalog else observation.raw_name, category_label)
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
                    dates = reference_groups.setdefault((value, unit), [])
                    label = ' '.join(filter(None, (column.date_label, column.report_label)))
                    if label not in dates:
                        dates.append(label)
        reference_ranges = tuple({'value': value or '未提供', 'unit': unit, 'dates': tuple(dates)}
                                 for (value, unit), dates in reference_groups.items())
        specimens = {identity[1] for identity in grouped if identity[0] == key[0]}
        specimen_label = (SPECIMEN_LABELS.get(key[1], key[1]) if key[1] else '标本待确认') if len(specimens) > 1 or not key[1] else ''
        rows.append(ComparisonRow(key[0], labels[key][0], labels[key][1],
                                  '', entries, () if multiple_series else sparkline, segments,
                                  len({point.observation.observation_date for point in sparkline}) >= 2,
                                  shared_unit, specimen_label, multiple_series, reference_ranges))
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
    groups = tuple(ComparisonGroup(key, tuple(value)) for key, value in sorted(category_rows.items()))
    from .presentation import CATEGORY_LABELS
    options = tuple((code, CATEGORY_LABELS.get(code, code)) for code in sorted(available_categories))
    return ComparisonView(columns, prioritize(rows, profile), reconciliation, prioritize_groups(groups, profile), options, pending_sources)
