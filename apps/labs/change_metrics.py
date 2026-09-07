"""Descriptive changes on the existing eligible comparison groups, without diagnosis."""

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from .numerics import calculate_numeric, supported_number


@dataclass(frozen=True)
class PersonalChange:
    previous: object = None
    baseline: tuple = ()
    elapsed_days: int | None = None
    absolute_change: Decimal | None = None
    daily_change: Decimal | None = None
    previous_percentage: Decimal | None = None
    baseline_mean: Decimal | None = None
    baseline_percentage: Decimal | None = None
    previous_reason: str = "没有更早的可比记录"
    baseline_reason: str = "此前不足三次可比记录"
    threshold_percent: int = 30
    highlight: bool = False

    @property
    def absolute_label(self):
        return _number_label(self.absolute_change, signed=True)

    @property
    def daily_label(self):
        return _number_label(self.daily_change, signed=True)

    @property
    def previous_percentage_label(self):
        return _number_label(self.previous_percentage, signed=True)

    @property
    def baseline_mean_label(self):
        return _number_label(self.baseline_mean)

    @property
    def baseline_percentage_label(self):
        return _number_label(self.baseline_percentage, signed=True)


def _number_label(value, *, signed=False):
    if value is None:
        return ""
    magnitude = value.copy_abs()
    scientific = magnitude >= Decimal('1e8') or (value != 0 and magnitude < Decimal('0.0001'))
    if scientific:
        return format(value, '+.3E' if signed else '.3E')
    return format(value, '+.2f' if signed else '.2f')


def _percentage(current, baseline):
    if baseline is None:
        return None, "数值超出可计算范围"
    if baseline <= 0:
        return None, "基准为零或负值，未计算百分比"
    value = calculate_numeric(lambda: (current - baseline) / baseline * 100)
    return value, "" if value is not None else "数值超出可计算范围"


def _change_for_cell(current, dated, *, latest, history):
    day = current.observation.observation_date
    threshold = current.change_threshold_percent
    if len(dated[day]) != 1:
        reason = "同日有多份可比结果，无法确定先后"
        return PersonalChange(previous_reason=reason, baseline_reason=reason, threshold_percent=threshold)

    values = {'threshold_percent': threshold}
    if latest:
        if len(latest) != 1:
            values['previous_reason'] = "上次日期有多份可比结果，未选择其中一份"
        else:
            previous = latest[0]
            elapsed = (day - previous.observation.observation_date).days
            percentage, reason = _percentage(current.numeric_value, previous.numeric_value)
            values.update(
                previous=previous,
                elapsed_days=elapsed,
                absolute_change=calculate_numeric(lambda: current.numeric_value - previous.numeric_value),
                daily_change=calculate_numeric(lambda: (current.numeric_value - previous.numeric_value) / elapsed),
                previous_percentage=percentage,
                previous_reason=reason,
            )

    if len(history) >= 3:
        baseline = history[-3:]
        if any(len(dated[cell.observation.observation_date]) != 1 for cell in baseline):
            values['baseline_reason'] = "近三次涉及同日多份结果，未确定个人基线"
        else:
            mean = calculate_numeric(lambda: sum((cell.numeric_value for cell in baseline), Decimal(0)) / 3)
            percentage, reason = _percentage(current.numeric_value, mean)
            values.update(
                baseline=baseline,
                baseline_mean=mean,
                baseline_percentage=percentage,
                baseline_reason=reason,
                highlight=percentage is not None and percentage.copy_abs() > threshold,
            )
    return PersonalChange(**values)


def changes_for_cells(cells):
    """Keep each observation, grouping only cells already approved for a trend.

    Callers supply current effective rows through comparable_cell; this function does
    not grant eligibility. Patient identity is part of the grouping even if a caller
    mistakenly supplies cells from more than one patient. Date-only duplicates stay
    ambiguous; no averaging or arbitrary previous observation resolves their order.
    """
    cells = tuple(cells)
    groups = defaultdict(lambda: defaultdict(list))
    result = {}
    for cell in cells:
        observation = cell.observation
        if (not cell.trend_eligible or not observation.observation_date
                or cell.numeric_value is None or not supported_number(cell.numeric_value)):
            result[str(observation.pk)] = PersonalChange(
                previous_reason="当前结果的可比依据不足",
                baseline_reason="当前结果的可比依据不足",
                threshold_percent=cell.change_threshold_percent,
            )
            continue
        key = (observation.parsing_version.document.patient_id, cell.group_key)
        groups[key][observation.observation_date].append(cell)
    for dated in groups.values():
        history = deque(maxlen=3)
        latest = ()
        for day in sorted(dated):
            same_day = dated[day]
            for cell in same_day:
                result[str(cell.observation.pk)] = _change_for_cell(cell, dated, latest=latest, history=tuple(history))
            # Append only after evaluating every result on the day. The full day
            # counts remain in dated even when an ambiguous row leaves this window.
            history.extend(same_day)
            latest = same_day
    return result
