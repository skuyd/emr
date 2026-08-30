import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from apps.processing.models import DatePrecision

from .models import CapabilityLevel, LabObservation, ResultType


_ORDINARY_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


@dataclass(frozen=True)
class TrendPoint:
    observation: LabObservation
    numeric_value: Decimal
    x: float = 0
    y: float = 0


@dataclass(frozen=True)
class TrendSeries:
    key: tuple[str, str, str]
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


def _normalized_text(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def _numeric_value(raw_value):
    normalized = unicodedata.normalize("NFKC", raw_value.strip()).replace("−", "-")
    if _ORDINARY_NUMBER.fullmatch(normalized) is None:
        return None
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _candidate_observations(patient, codes):
    return tuple(
        LabObservation.objects.filter(
            parsing_version__active=True,
            parsing_version__document__patient=patient,
            parsing_version__document__deleted_at__isnull=True,
            parsing_version__document_summary__date_precision=DatePrecision.DAY,
            standard_code__in=codes,
            capability_level=CapabilityLevel.STABLE,
            result_type=ResultType.NUMERIC,
            observation_date__isnull=False,
        )
        .select_related(
            "document_page",
            "evidence",
            "evidence__document_page",
            "parsing_version__document",
        )
        .order_by("standard_code", "observation_date", "parsing_version__document__created_at", "reading_order", "pk")
    )


def _positioned(points):
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


def _series_for_code(observations):
    by_unit = defaultdict(list)
    for observation, numeric_value in observations:
        by_unit[observation.raw_unit.strip()].append((observation, numeric_value))

    series = []
    for unit, unit_rows in by_unit.items():
        institution_methods = defaultdict(set)
        method_display = {}
        institution_display = {}
        for observation, _numeric in unit_rows:
            institution = _normalized_text(observation.institution_raw)
            method = _normalized_text(observation.method_raw)
            if institution:
                institution_display.setdefault(institution, observation.institution_raw.strip())
            if method:
                method_display.setdefault(method, observation.method_raw.strip())
                if institution:
                    institution_methods[institution].add(method)

        grouped = defaultdict(list)
        for observation, numeric_value in unit_rows:
            institution = _normalized_text(observation.institution_raw)
            method = _normalized_text(observation.method_raw)
            if method:
                key = (unit, "method", method)
            elif institution and len(institution_methods[institution]) == 1:
                key = (unit, "method", next(iter(institution_methods[institution])))
            elif institution and not institution_methods[institution]:
                key = (unit, "institution", institution)
            else:
                continue
            grouped[key].append(TrendPoint(observation=observation, numeric_value=numeric_value))

        for key, points in grouped.items():
            if len(points) < 2 or len({point.observation.observation_date for point in points}) < 2:
                continue
            positioned = _positioned(tuple(points))
            if key[1] == "method":
                basis_label = f"报告方法：{method_display[key[2]]}"
            else:
                basis_label = f"同一机构：{institution_display[key[2]]}"
            series.append(
                TrendSeries(
                    key=key,
                    unit=unit,
                    basis_label=basis_label,
                    points=positioned,
                    polyline=" ".join(f"{point.x},{point.y}" for point in positioned),
                )
            )
    return tuple(sorted(series, key=lambda item: item.key))


def _trend_views(patient, codes):
    rows_by_code = defaultdict(list)
    for observation in _candidate_observations(patient, codes):
        numeric_value = _numeric_value(observation.raw_value)
        if numeric_value is not None:
            rows_by_code[observation.standard_code].append((observation, numeric_value))

    views = {}
    for code, observations in rows_by_code.items():
        series = _series_for_code(observations)
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
