"""Record-level curves and multi-value cells without averaging or clinical cutoffs."""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext
import hashlib

from .payloads import CALCULATION, TIME_SLOTS


SOURCE_LABELS = {'MANUAL': '手动记录', 'METER': '血糖仪自测', 'NURSING': '护理原件录入', 'LAB_REPORT': '检验单'}


def display_row(record, *, source_available=True):
    data = deepcopy(record.current_data)
    usable = record.deleted_at is None and source_available
    status = '已删除' if record.deleted_at is not None else '来源已变化，请重新核对原件' if not source_available else ''
    zone_label = (f"{data['timezone']} (UTC{data['utc_offset']})" if data['utc_offset'] else
                  f"{data['timezone']}（未生成 UTC 时刻）") if data['timezone'] else '时区未确认'
    return {'record': record, 'data': data, 'label': f"{data['raw_value']} {data['raw_unit']}".strip(),
            'source_kind_label': SOURCE_LABELS[record.source_kind], 'source_label': data.get('source_label', ''),
            'time_label': data['local_time'].replace('T', ' ') if data['local_time'] else '时间不详', 'date_label': data['measured_date'] or '日期不详',
            'timezone_label': zone_label, 'slot_label': TIME_SLOTS[data['time_slot']],
            'usable': usable, 'source_available': source_available, 'status': status}


def _group(row):
    data, source = row['data'], row['data'].get('source', {})
    return (data['source_kind'], data.get('source_label', ''),
            source.get('report_context', {}).get('specimen_raw', ''),
            source.get('field_sources', {}).get('method_raw', {}).get('effective_value', ''),
            data['timezone'], data['utc_offset'], data['normalized_unit'])


def _numeric(row):
    data = row['data']
    if data.get('source', {}).get('measurement_scope') == 'SUMMARY':
        return None
    return Decimal(data['normalized_value']) if data['normalized_value'] is not None else None


def _segments(points):
    segments, current = [], []
    for point in points:
        if point['same_time_count'] > 1:
            if len(current) > 1:
                segments.append(' '.join(f"{item['x']},{item['y']}" for item in current))
            current = []
        else:
            current.append(point)
    if len(current) > 1:
        segments.append(' '.join(f"{item['x']},{item['y']}" for item in current))
    return segments


def history_charts(rows):
    groups = defaultdict(list)
    omitted = []
    for row in rows:
        if row['usable']:
            groups[_group(row)].append(row)
        if not row['usable'] or not row['data']['plot_eligible'] or _numeric(row) is None:
            omitted.append(row)
    charts, heatmaps = [], []
    for key, grouped in groups.items():
        identity = hashlib.sha256(repr(key).encode()).hexdigest()[:16]
        title = ' · '.join(dict.fromkeys(filter(None, [SOURCE_LABELS[key[0]], *key[1:4], grouped[0]['timezone_label']])))
        values = [value for row in grouped if (value := _numeric(row)) is not None]
        low, high = (min(values), max(values)) if values else (None, None)
        heat_days = defaultdict(lambda: defaultdict(list))
        plotted = []
        with localcontext(CALCULATION):
            for row in grouped:
                value = _numeric(row)
                shade = None if value is None else 2 if high == low else min(4, int((value - low) / (high - low) * 5))
                heat_days[row['date_label']][row['data']['time_slot']].append({**row, 'shade': shade})
                if value is None or not row['data']['plot_eligible']:
                    continue
                local = datetime.fromisoformat(row['data']['local_time'])
                seconds = local.hour * 3600 + local.minute * 60 + local.second
                y = Decimal(100) if high == low else Decimal(180) - (value - low) / (high - low) * 160
                plotted.append({'row': row, 'record': row['record'], 'day': local.date().isoformat(),
                    'seconds': seconds, 'value': row['data']['normalized_value'], 'x': round(50 + seconds / 86400 * 720, 3),
                    'y': float(round(y, 3)), 'label': f"{row['time_label']} · {row['label']} · {row['slot_label']}"})
        days = []
        for day in sorted(heat_days):
            days.append({'day': day, 'cells': [{'slot': slot, 'label': label, 'rows': heat_days[day][slot]}
                                              for slot, label in TIME_SLOTS.items()]})
        heatmaps.append({'key': identity, 'title': title, 'days': days,
                         'low': str(low) if low is not None else None, 'high': str(high) if high is not None else None})
        if not plotted:
            continue
        series = []
        for index, day in enumerate(sorted({point['day'] for point in plotted})):
            # Python's stable sort preserves every supplied record at a tie;
            # tied timestamps break the line and have no inferred order.
            points = sorted([point for point in plotted if point['day'] == day], key=lambda point: point['seconds'])
            counts = Counter(point['seconds'] for point in points)
            for point in points:
                point['same_time_count'] = counts[point['seconds']]
            series.append({'day': day, 'style': index % 6, 'points': points, 'segments': _segments(points)})
        charts.append({'key': identity, 'title': title, 'unit': 'mmol/L', 'low': str(low), 'high': str(high),
                       'series': series, 'points_count': len(plotted),
                       'ticks': [{'hour': hour, 'x': 50 + hour * 30} for hour in (0, 6, 12, 18, 24)]})
    return {'charts': charts, 'heatmaps': heatmaps, 'omitted': omitted,
            'slots': [{'value': key, 'label': label} for key, label in TIME_SLOTS.items()]}
