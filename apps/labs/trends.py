import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException, localcontext

from .models import LabObservation
from .numerics import CALCULATION_CONTEXT, numeric_value as _numeric_value


_ORDINARY_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


@dataclass(frozen=True)
class TrendPoint:
    observation: LabObservation
    numeric_value: Decimal
    x: float = 0
    y: float = 0
    converted_unit: str = ""
    conversion_rule: object = None


@dataclass(frozen=True)
class TrendSeries:
    key: tuple[str, ...]
    unit: str
    basis_label: str
    points: tuple[TrendPoint, ...]
    polyline: str


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


def _positioned(points):
    try:
        with localcontext(CALCULATION_CONTEXT):
            return _calculate_positions(points)
    except (DecimalException, ZeroDivisionError, OverflowError):
        return ()


def _calculate_positions(points):
    first_date = min(point.observation.observation_date.toordinal() for point in points)
    last_date = max(point.observation.observation_date.toordinal() for point in points)
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


def _series_for_code(observations, *, previous=()):
    from .comparison import comparable_cell

    grouped = defaultdict(list)
    cells = {}
    all_rows = previous or tuple(row for row, _ in observations)
    for observation, _numeric in observations:
        cell = comparable_cell(observation, previous=all_rows)
        if not cell.trend_eligible:
            continue
        grouped[cell.group_key].append(TrendPoint(observation, cell.numeric_value, converted_unit=cell.unit if cell.rule else "", conversion_rule=cell.rule))
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
                                  " ".join(f"{point.x},{point.y}" for point in positioned)))
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
