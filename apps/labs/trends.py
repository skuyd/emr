from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .models import LabObservation


@dataclass(frozen=True)
class TrendPoint:
    observation: LabObservation
    numeric_value: Decimal
    conversion_rule: object = None


@dataclass(frozen=True)
class TrendSeries:
    key: tuple[str, ...]
    unit: str
    basis_label: str
    points: tuple[TrendPoint, ...]


def _series_for_code(observations, *, previous=()):
    """Build comparable daily numeric series used by export snapshots."""
    from .comparison import comparable_cell
    from .comparison_policy import SPECIMEN_LABELS
    from .consolidation import institution_key, latest_daily_cells

    grouped = defaultdict(list)
    cells = {}
    all_rows = previous or tuple(row for row, _ in observations)
    comparison_cells = tuple(comparable_cell(observation, previous=all_rows) for observation, _numeric in observations)
    daily, _disputed = latest_daily_cells(comparison_cells)
    for cell in daily:
        if not cell.trend_eligible:
            continue
        observation = cell.observation
        key = cell.group_key + institution_key(observation)
        grouped[key].append(TrendPoint(observation, cell.numeric_value, conversion_rule=cell.rule))
        cells[key] = cell
    series = []
    for key, points in grouped.items():
        if len({point.observation.observation_date for point in points}) < 2:
            continue
        points.sort(key=lambda point: (point.observation.observation_date, point.observation.created_at, str(point.observation.pk)))
        cell = cells[key]
        basis = cell.observation.comparison_institution + ' · 标本：' + SPECIMEN_LABELS.get(key[1], key[1] or '标本待确认')
        if cell.method_rule:
            basis += f" · 可比规则 {cell.method_rule['id']} / {cell.method_rule['version']}：{cell.method_rule['rationale']}"
        elif cell.observation.method_raw:
            basis += ' · 报告方法：' + cell.observation.method_raw
        series.append(TrendSeries(key, cell.unit, basis, tuple(points)))
    return tuple(sorted(series, key=lambda item: item.key))
