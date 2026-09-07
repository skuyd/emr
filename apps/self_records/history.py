"""Display coordinates use actual instants; stored measurements stay untouched."""

from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from .models import DailyRecord
from .payloads import CALCULATION


def display_row(record, zone_name):
    data = record.current_data
    if record.kind == 'SYMPTOM':
        label = ' · '.join(value for value in (data['symptom_name'], data['severity']) if value)
    else:
        label = f"{data['raw_value']} {data['raw_unit']}"
    return {'record': record, 'id': str(record.pk), 'label': label, 'data': data,
            'display_time': record.measured_at.astimezone(ZoneInfo(zone_name)).strftime('%Y-%m-%d %H:%M'),
            'kind_label': record.get_kind_display()}


def history_series(records, zone_name):
    ordered = sorted(records, key=lambda record: (record.measured_at, str(record.pk)))
    if not ordered:
        return []
    first, last = ordered[0].measured_at, ordered[-1].measured_at
    duration = (last - first).total_seconds()
    charts = []
    for kind, title in DailyRecord.Kind.choices:
        selected = [record for record in ordered if record.kind == kind]
        if not selected:
            continue
        points = []
        with localcontext(CALCULATION):
            values = [Decimal(record.current_data['normalized_value']) for record in selected] if kind != 'SYMPTOM' else []
            low, high = (min(values), max(values)) if values else (Decimal(0), Decimal(0))
            for index, record in enumerate(selected):
                point = display_row(record, zone_name)
                point['x'] = 50 + 700 * (record.measured_at - first).total_seconds() / duration if duration else 400
                value = values[index] if values else None
                point['y'] = float(170 - (value - low) * 130 / (high - low)) if values and high != low else 105
                point['value'] = record.current_data['normalized_value']
                points.append(point)
        charts.append({'kind': kind, 'title': title, 'unit': selected[0].current_data['normalized_unit'],
                       'points': points, 'low': str(low), 'high': str(high),
                       'first_time': display_row(ordered[0], zone_name)['display_time'],
                       'last_time': display_row(ordered[-1], zone_name)['display_time']})
    return charts
