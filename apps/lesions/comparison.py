"""Source-preserving dimension/SUV comparison, without medical interpretation.

The caller supplies authorized, source-current observation material. Ordered
components remain independent: neither their order nor their numeric maximum
establishes an axis. Missing context is retained for the table and breaks a line.
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext


@dataclass(frozen=True)
class MeasurementPoint:
    observation_id: str
    lesion_id: str
    report_id: str
    field_id: str
    kind: str
    component_index: int
    axis: str | None
    role: str
    date_value: str | None
    date_reliable: bool
    method: tuple[str, str]
    field_conflict: bool
    raw_value: str | None
    raw_unit: str | None
    raw_expression: str
    value: Decimal | None
    range_values: tuple[Decimal, ...]
    unit: str | None
    comparator: str
    approximate: bool
    conversion: dict | None
    report_maximum: bool

    @property
    def current_plot_eligible(self):
        # Unknown axes/methods retain separately labelled points, but cannot
        # establish a comparable line or arithmetic change between reports.
        return not (set(_point_blockers(self)) - {"axis_unknown", "method_unknown"})


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _day(value, precision):
    if precision != "DAY" or not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def measurement_points(observation):
    """Return usable measurement values with their literal expressions intact."""
    if not observation.get("usable"):
        return []
    fields = [field for field in observation.get("fields", ()) if field.get("usable")]
    maximum_fields = [field for field in fields if field["field_key"] == "lesion.maximum_scope"]
    report_maximum = bool(maximum_fields) and all(
        not field.get("conflict") and field["content"]["value"].get("code") == "REPORT_MAXIMUM"
        for field in maximum_fields
    )
    context_date = observation.get("date") or {}
    context_method = observation.get("method") or {}
    method = (context_method.get("code") or "", context_method.get("raw") or "")
    points = []
    for field in fields:
        key = field["field_key"]
        if key not in {"lesion.dimensions", "lesion.suvmax"}:
            continue
        value = field["content"]["value"]
        common = dict(
            observation_id=observation["id"], lesion_id=observation.get("lesion_id") or "",
            report_id=observation["report_id"], field_id=field["id"],
            role=value.get("measurement_role", "UNKNOWN"), date_value=context_date.get("value"),
            date_reliable=_day(context_date.get("value"), context_date.get("precision")),
            method=method, field_conflict=bool(field.get("conflict")),
            raw_expression=value.get("raw", ""), approximate=bool(value.get("approximate")),
            report_maximum=report_maximum,
        )
        if key == "lesion.dimensions":
            components = value.get("components", ())
            axes = [component.get("axis") for component in components]
            for index, component in enumerate(components):
                raw_value, raw_unit = component.get("value"), component.get("unit")
                number = _number(raw_value)
                conversion = None
                unit = raw_unit
                if raw_unit in {"cm", "厘米"}:
                    if number is not None:
                        literal = number.as_tuple()
                        number = Decimal((literal.sign, literal.digits, literal.exponent + 1))
                    unit = "mm"
                    conversion = {"factor": "10", "from": raw_unit, "to": "mm",
                                  "rule": "metric_length_cm_to_mm_v1"}
                elif raw_unit in {"mm", "毫米"}:
                    unit = "mm"
                    if raw_unit != unit:
                        conversion = {"factor": "1", "from": raw_unit, "to": "mm",
                                      "rule": "metric_length_unit_alias_v1"}
                axis = component.get("axis")
                point = MeasurementPoint(
                    **common, kind="DIMENSION", component_index=index, axis=axis,
                    raw_value=raw_value, raw_unit=raw_unit, value=number, unit=unit,
                    range_values=(), comparator="EQ", conversion=conversion,
                )
                if axis and axes.count(axis) > 1:
                    point = replace(point, field_conflict=True)
                points.append(point)
        else:
            raw_values = value.get("values", ())
            numbers = tuple(_number(number) for number in raw_values)
            comparator = value.get("comparator", "UNKNOWN")
            scalar = numbers[0] if comparator == "EQ" and len(numbers) == 1 else None
            bounds = numbers if comparator == "RANGE" and len(numbers) == 2 and None not in numbers else ()
            points.append(MeasurementPoint(
                **common, kind="SUV", component_index=0, axis=None,
                raw_value=raw_values[0] if len(raw_values) == 1 else None,
                raw_unit=value.get("unit"), value=scalar, unit=value.get("unit"),
                range_values=bounds, comparator=comparator, conversion=None,
            ))
    return points


def _point_blockers(point):
    reasons = []
    if not point.lesion_id:
        reasons.append("unassigned_observation")
    if point.role != "CURRENT":
        reasons.append("historical_or_unknown_role")
    if point.comparator != "EQ":
        reasons.append("bounded_value")
    elif point.value is None:
        reasons.append("value_unavailable")
    if point.field_conflict:
        reasons.append("field_conflict")
    if not point.date_reliable:
        reasons.append("date_unreliable")
    if not all(point.method):
        reasons.append("method_unknown")
    if point.kind == "DIMENSION":
        if not point.axis:
            reasons.append("axis_unknown")
        if point.unit != "mm":
            reasons.append("unit_unrecognized")
    return reasons


def compare_measurements(previous, current):
    """Compute only an arithmetic difference between compatible observations."""
    reasons = list(dict.fromkeys(_point_blockers(previous) + _point_blockers(current)))
    if previous.lesion_id != current.lesion_id:
        reasons.append("lesion_changed")
    if previous.report_id == current.report_id:
        reasons.append("same_report")
    if previous.kind != current.kind:
        reasons.append("measurement_kind_changed")
    elif previous.kind == "DIMENSION" and previous.axis and current.axis and previous.axis != current.axis:
        reasons.append("axis_changed")
    if previous.method != current.method:
        reasons.append("method_changed")
    if previous.unit != current.unit:
        reasons.append("unit_changed")
    if previous.date_reliable and current.date_reliable and previous.date_value >= current.date_value:
        reasons.append("date_not_later")
    if reasons:
        return {"comparable": False, "reasons": tuple(reasons), "delta": None}
    # Decimal's global precision must not silently round reported digits.
    with localcontext() as context:
        context.prec = max(context.prec, max(current.value.adjusted(), previous.value.adjusted(), 0)
                           - min(current.value.as_tuple().exponent, previous.value.as_tuple().exponent, 0) + 2)
        difference = current.value - previous.value
    return {"comparable": True, "reasons": (), "delta": difference}
