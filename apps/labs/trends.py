import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
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

    @property
    def source_url(self):
        parts = urlsplit(self.observation.source_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query['evidence'] = str(self.observation.evidence.pk)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


@dataclass(frozen=True)
class TrendSeries:
    key: tuple[str, ...]
    unit: str
    basis_label: str
    points: tuple[TrendPoint, ...]
    polyline: str
    segments: tuple = ()

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


@dataclass(frozen=True)
class TrendSummary:
    standard_code: str
    standard_name: str
    latest_observation: LabObservation
    point_count: int


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


def _line_segments(points):
    counts = defaultdict(int)
    for point in points:
        counts[point.observation.observation_date] += 1
    segments, current = [], []
    for point in points:
        if counts[point.observation.observation_date] > 1:
            if len(current) > 1:
                segments.append(' '.join(f'{item.x},{item.y}' for item in current))
            current = []
        else:
            current.append(point)
    if len(current) > 1:
        segments.append(' '.join(f'{item.x},{item.y}' for item in current))
    return tuple(segments)


def _series_for_code(observations, *, previous=()):
    from .comparison import comparable_cell

    grouped = defaultdict(list)
    cells = {}
    all_rows = previous or tuple(row for row, _ in observations)
    comparison_cells = tuple(comparable_cell(observation, previous=all_rows) for observation, _numeric in observations)
    changes = changes_for_cells(comparison_cells)
    for cell in comparison_cells:
        observation = cell.observation
        if not cell.trend_eligible:
            continue
        grouped[cell.group_key].append(TrendPoint(observation, cell.numeric_value, converted_unit=cell.unit if cell.rule else "", conversion_rule=cell.rule,
                                                 change=changes[str(observation.pk)]))
        cells[cell.group_key] = cell
    series = []
    for key, points in grouped.items():
        if len(points) < 2 or len({point.observation.observation_date for point in points}) < 2:
            continue
        points.sort(key=lambda point: (point.observation.observation_date, point.observation.created_at, str(point.observation.pk)))
        positioned = _positioned(tuple(points))
        if not positioned:
            continue
        series.append(TrendSeries(key, cells[key].unit, f"标本：{key[1]} · 报告方法：{key[3]}", positioned,
                                  " ".join(f"{point.x},{point.y}" for point in positioned), _line_segments(positioned)))
    return tuple(sorted(series, key=lambda item: item.key))


def _trend_views(patient, codes=None):
    rows_by_code = defaultdict(list)
    candidates = _candidate_observations(patient)
    for observation in candidates:
        if codes is not None and observation.standard_code not in codes:
            continue
        numeric_value = _numeric_value(observation.raw_value)
        if numeric_value is not None:
            rows_by_code[observation.standard_code].append((observation, numeric_value))

    views = {}
    for code, observations in rows_by_code.items():
        series = _series_for_code(observations, previous=candidates)
        if not series:
            continue
        included = [point.observation for item in series for point in item.points]
        latest = max(included, key=lambda item: (item.observation_date, item.created_at, str(item.pk)))
        raw_names = tuple(dict.fromkeys(item.raw_name for item in included))
        views[code] = TrendView(
            standard_code=code,
            standard_name=latest.standard_name,
            raw_names=raw_names,
            series=series,
        )
    return views


def eligible_trend_codes(patient, codes):
    codes = tuple(dict.fromkeys(codes))
    return frozenset(_trend_views(patient, codes)) if codes else frozenset()


def trend_view(patient, standard_code):
    return _trend_views(patient, (standard_code,)).get(standard_code)


def joint_trend_views(patient, codes, *, start=None, end=None):
    """Small multiples share a date axis; values retain each comparable group's unit."""
    codes = tuple(dict.fromkeys(codes))
    candidates = _trend_views(patient, codes)
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
                                      segments=_line_segments(positioned)))
        if series:
            views.append(replace(trend, series=tuple(series)))
    return tuple(views), bounds


def trend_summaries(patient):
    summaries = []
    for trend in _trend_views(patient).values():
        included = tuple(point.observation for series in trend.series for point in series.points)
        latest = max(included, key=lambda item: (item.observation_date, item.created_at, str(item.pk)))
        summaries.append(TrendSummary(trend.standard_code, trend.standard_name, latest, len(included)))
    return tuple(
        sorted(
            summaries,
            key=lambda item: (
                -item.latest_observation.observation_date.toordinal(),
                item.standard_name.casefold(),
                item.standard_code,
            ),
        )
    )
