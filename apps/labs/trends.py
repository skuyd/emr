import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, DecimalException, localcontext
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import LabObservation
from .numerics import CALCULATION_CONTEXT, numeric_value as _numeric_value
from .change_metrics import changes_for_cells


_ORDINARY_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


@dataclass(frozen=True)
class TrendPoint:
    observation: LabObservation
    numeric_value: Decimal
    x: float = 0
    y: float = 0
    converted_unit: str = ""
    conversion_rule: object = None
    change: object = None
    sources: tuple = ()
    catalog: object = None

    @property
    def display_value(self):
        return self.catalog.value.display_value if self.catalog else self.observation.raw_value

    @property
    def display_unit(self):
        return self.catalog.value.unit if self.catalog else self.observation.raw_unit

    @property
    def source_url(self):
        parts = urlsplit(self.observation.source_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query['evidence'] = str(self.observation.evidence.pk)
        query['patient'] = str(self.observation.parsing_version.document.patient_id)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@dataclass(frozen=True)
class TrendSeries:
    key: tuple[str, ...]
    unit: str
    basis_label: str
    points: tuple[TrendPoint, ...]
    polyline: str
    segments: tuple = ()
    historical: bool = False
    blocked_dates: tuple = ()

    @property
    def minimum(self):
        return min(point.numeric_value for point in self.points)

    @property
    def maximum(self):
        return max(point.numeric_value for point in self.points)

    @property
    def minimum_label(self):
        return str(self.minimum)

    @property
    def maximum_label(self):
        return str(self.maximum)


@dataclass(frozen=True)
class TrendView:
    standard_code: str
    standard_name: str
    raw_names: tuple[str, ...]
    series: tuple[TrendSeries, ...]
    daily_details: tuple = ()
    disputed: tuple = ()
    raw_name: str = ''


@dataclass(frozen=True)
class TrendSummary:
    standard_code: str
    standard_name: str
    latest_observation: LabObservation
    point_count: int
    display_value: str = ''
    display_unit: str = ''


def _normalized_text(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def _candidate_observations(patient, codes=None):
    from .readmodels import effective_rows

    return tuple(row for row in effective_rows(patient) if codes is None or row.standard_code in codes)


def _positioned(points, *, date_bounds=None):
    try:
        with localcontext(CALCULATION_CONTEXT):
            return _calculate_positions(points, date_bounds=date_bounds)
    except (DecimalException, ZeroDivisionError, OverflowError):
        return ()


def _calculate_positions(points, *, date_bounds=None):
    first_date = date_bounds[0].toordinal() if date_bounds else min(point.observation.observation_date.toordinal() for point in points)
    last_date = date_bounds[1].toordinal() if date_bounds else max(point.observation.observation_date.toordinal() for point in points)
    low = min(point.numeric_value for point in points)
    high = max(point.numeric_value for point in points)
    date_span = max(1, last_date - first_date)
    value_span = high - low
    positioned = []
    for point in points:
        day = point.observation.observation_date.toordinal()
        x = 7 + (day - first_date) * 86 / date_span
        y = 45 if value_span == 0 else 82 - float((point.numeric_value - low) / value_span) * 68
        positioned.append(replace(point, x=round(x, 3), y=round(y, 3)))
    return tuple(positioned)


def _blocked_dates(cells, series_cell):
    from .consolidation import _daily_key

    series_key = _daily_key(series_cell)
    dates = set()
    for cell in cells:
        key = _daily_key(cell)
        if (key[0], *key[2:]) == (series_key[0], *series_key[2:]):
            dates.update((key[1],) if key[1] else cell.observation.report_identity.sampling_dates)
    return tuple(sorted(dates))


def _line_segments(points, *, blocked_dates=()):
    counts = defaultdict(int)
    for point in points:
        counts[point.observation.observation_date] += 1
    segments, current = [], []
    for point in points:
        if current and any(current[-1].observation.observation_date < day <= point.observation.observation_date
                           for day in blocked_dates):
            if len(current) > 1:
                segments.append(' '.join(f'{item.x},{item.y}' for item in current))
            current = []
        if counts[point.observation.observation_date] > 1:
            if len(current) > 1:
                segments.append(' '.join(f'{item.x},{item.y}' for item in current))
            current = []
        else:
            current.append(point)
    if len(current) > 1:
        segments.append(' '.join(f'{item.x},{item.y}' for item in current))
    return tuple(segments)


def _series_for_code(observations, *, previous=(), include_history=False, start=None, end=None):
    from .comparison import comparable_cell
    from .consolidation import institution_key, latest_daily_cells

    grouped = defaultdict(list)
    cells = {}
    all_rows = previous or tuple(row for row, _ in observations)
    comparison_cells = tuple(comparable_cell(observation, previous=all_rows) for observation, _numeric in observations)
    daily, disputed = latest_daily_cells(comparison_cells)
    blockers = (*disputed, *(cell for cell in daily if not cell.trend_eligible))
    changes = changes_for_cells(daily)
    for cell in daily:
        observation = cell.observation
        if not (cell.plot_eligible if include_history else cell.trend_eligible):
            continue
        key = cell.group_key + institution_key(observation) + (('historical',) if not cell.trend_eligible else ())
        grouped[key].append(TrendPoint(observation, cell.numeric_value, converted_unit=cell.unit if cell.rule else "", conversion_rule=cell.rule,
                                                 change=changes[str(observation.pk)], sources=cell.sources, catalog=cell.catalog))
        cells[key] = cell
    series = []
    for key, points in grouped.items():
        points = [point for point in points if (not start or point.observation.observation_date >= start)
                  and (not end or point.observation.observation_date <= end)]
        if not points or (not include_history and len({point.observation.observation_date for point in points}) < 2):
            continue
        points.sort(key=lambda point: (point.observation.observation_date, point.observation.created_at, str(point.observation.pk)))
        positioned = _positioned(tuple(points))
        if not positioned:
            continue
        from .comparison_policy import SPECIMEN_LABELS
        cell = cells[key]
        historical = not cell.trend_eligible
        basis = cell.observation.comparison_institution + ' · 标本：' + SPECIMEN_LABELS.get(key[1], key[1] or '标本待确认')
        if cell.method_rule:
            basis += f" · 可比规则 {cell.method_rule['id']} / {cell.method_rule['version']}：{cell.method_rule['rationale']}"
        elif cell.observation.method_raw:
            basis += ' · 报告方法：' + cell.observation.method_raw
        if historical:
            basis += ' · 历史记录点：尚无适用的可比规则，不连线或计算个人变化'
        blocked_dates = _blocked_dates(blockers, cell)
        series.append(TrendSeries(key, cell.unit, basis, positioned,
                                  " ".join(f"{point.x},{point.y}" for point in positioned),
                                  () if historical else _line_segments(positioned, blocked_dates=blocked_dates),
                                  historical, blocked_dates))
    return tuple(sorted(series, key=lambda item: item.key))


def _trend_views(patient, codes=None, *, include_history=False, start=None, end=None, raw_name=None):
    from .catalog import load_catalog
    from .catalog_projection import project_catalog
    rows_by_code = defaultdict(list)
    candidates = _candidate_observations(patient)
    catalog_names = {}
    catalog_codes = {item.code for item in load_catalog().indicators}
    for observation in candidates:
        projected = project_catalog(observation)
        code = projected.indicator.code if projected else observation.standard_code
        if raw_name is not None:
            if projected or observation.raw_name.strip().casefold() != raw_name.strip().casefold():
                continue
        elif projected is None and code in catalog_codes:
            continue
        if projected:
            catalog_names[code] = projected.indicator.name
        if codes is not None and code not in codes:
            continue
        numeric_value = _numeric_value(observation.raw_value)
        rows_by_code[code].append((observation, numeric_value))

    views = {}
    for code, observations in rows_by_code.items():
        series = _series_for_code(observations, previous=candidates, include_history=include_history, start=start, end=end)
        from .comparison import comparable_cell
        from .consolidation import fold_cells, latest_daily_cells
        visible = [row for row, _ in observations if (not start or row.observation_date and row.observation_date >= start)
                   and (not end or row.observation_date and row.observation_date <= end)]
        if not visible:
            continue
        detail_cells = tuple(comparable_cell(row, previous=candidates) for row in visible)
        _, disputed = latest_daily_cells(detail_cells)
        if not series and not include_history and not disputed:
            continue
        included = [point.observation for item in series for point in item.points] or visible
        latest = max(included, key=lambda item: (item.observation_date or date.min, item.created_at, str(item.pk)))
        raw_names = tuple(dict.fromkeys(item.raw_name for item in included))
        views[code] = TrendView(
            standard_code=code,
            standard_name=catalog_names.get(code, latest.raw_name),
            raw_names=raw_names,
            series=series,
            daily_details=fold_cells(detail_cells),
            disputed=disputed,
            raw_name=raw_name or '',
        )
    return views


def eligible_trend_codes(patient, codes):
    codes = tuple(dict.fromkeys(codes))
    return frozenset(code for code, view in _trend_views(patient, codes).items() if view.series) if codes else frozenset()


def trend_view(patient, standard_code, *, include_history=False, start=None, end=None, raw_name=None):
    return _trend_views(patient, (standard_code,), include_history=include_history, start=start, end=end, raw_name=raw_name).get(standard_code)


def joint_trend_views(patient, codes, *, start=None, end=None):
    """Small multiples share a date axis; values retain each comparable group's unit."""
    codes = tuple(dict.fromkeys(codes))
    candidates = _trend_views(patient, codes, start=start, end=end)
    retained = []
    for code in codes:
        trend = candidates.get(code)
        if trend is None:
            continue
        series = []
        for group in trend.series:
            points = tuple(point for point in group.points
                           if (start is None or point.observation.observation_date >= start)
                           and (end is None or point.observation.observation_date <= end))
            if len({point.observation.observation_date for point in points}) >= 2:
                series.append(replace(group, points=points))
        if series:
            retained.append(replace(trend, series=tuple(series)))
    all_points = [point for trend in retained for series in trend.series for point in series.points]
    if not all_points:
        return (), None
    bounds = (start or min(point.observation.observation_date for point in all_points),
              end or max(point.observation.observation_date for point in all_points))
    views = []
    for trend in retained:
        series = []
        for group in trend.series:
            positioned = _positioned(group.points, date_bounds=bounds)
            if positioned:
                series.append(replace(group, points=positioned, polyline=' '.join(f'{point.x},{point.y}' for point in positioned),
                                      segments=_line_segments(positioned, blocked_dates=group.blocked_dates)))
        if series:
            views.append(replace(trend, series=tuple(series)))
    return tuple(views), bounds


def trend_summaries(patient, *, ordering_profile=None):
    from apps.cancer_ordering.profiles import prioritize
    from apps.cancer_ordering.readmodels import resolve_ordering

    profile = ordering_profile if ordering_profile is not None else resolve_ordering(patient)['profile']
    summaries = []
    for trend in _trend_views(patient).values():
        included = tuple(point.observation for series in trend.series for point in series.points)
        if not included:
            continue
        latest = max(included, key=lambda item: (item.observation_date, item.created_at, str(item.pk)))
        point = next(point for series in trend.series for point in series.points if point.observation.pk == latest.pk)
        summaries.append(TrendSummary(trend.standard_code, trend.standard_name, latest,
                                      sum(len(cell.sources) for cell in trend.daily_details), point.display_value, point.display_unit))
    return prioritize(
        sorted(
            summaries,
            key=lambda item: (
                -item.latest_observation.observation_date.toordinal(),
                item.standard_name.casefold(),
                item.standard_code,
            ),
        ), profile,
    )
