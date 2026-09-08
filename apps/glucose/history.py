"""Record-level curves and multi-value cells without averaging or clinical cutoffs."""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext
import hashlib
from zoneinfo import ZoneInfo

from .payloads import CALCULATION, TIME_SLOTS


SOURCE_LABELS = {'MANUAL': '手动记录', 'METER': '血糖仪自测', 'NURSING': '护理原件录入', 'LAB_REPORT': '检验单'}


def _display_time(data, display_timezone):
    result = {key: data[key] for key in ('local_time', 'measured_date', 'timezone', 'utc_offset')}
    result.update(converted=False, note='')
    if not display_timezone:
        return result
    if not data['measured_at'] or data['time_precision'] not in ('MINUTE', 'SECOND'):
        result['note'] = '没有确定时刻，保留原时间与日期，未做时区换算。'
        return result
    try:
        local = datetime.fromisoformat(data['measured_at']).astimezone(ZoneInfo(display_timezone))
    except OverflowError:
        result['note'] = '所选时区无法表示这个日期，保留原记录时间。'
        return result
    offset = local.strftime('%z')
    return {**result, 'local_time': local.replace(tzinfo=None).isoformat(
                timespec='seconds' if data['time_precision'] == 'SECOND' or local.second else 'minutes'),
            'measured_date': local.date().isoformat(), 'timezone': display_timezone,
            'utc_offset': offset[:3] + ':' + offset[3:5] + (':' + offset[5:] if len(offset) > 5 else ''),
            'converted': True}


def _zone_label(data):
    return ((f"{data['timezone']} (UTC{data['utc_offset']})" if data['utc_offset'] else
             f"{data['timezone']}（未生成 UTC 时刻）") if data['timezone'] else '时区未确认')


def display_row(record, *, source_available=True, display_timezone=''):
    data = deepcopy(record.current_data)
    displayed = _display_time(data, display_timezone)
    usable = record.deleted_at is None and source_available
    status = '已删除' if record.deleted_at is not None else '来源已变化，请重新核对原件' if not source_available else ''
    return {'record': record, 'data': data, 'label': f"{data['raw_value']} {data['raw_unit']}".strip(),
            'source_kind_label': SOURCE_LABELS[record.source_kind], 'source_label': data.get('source_label', ''),
            'time_label': displayed['local_time'].replace('T', ' ') if displayed['local_time'] else '时间不详',
            'date_label': displayed['measured_date'] or '日期不详', 'display_time': displayed,
            'time_converted': displayed['converted'], 'conversion_note': displayed['note'],
            'original_time_label': data['local_time'].replace('T', ' ') if data['local_time'] else '时间不详',
            'original_timezone_label': _zone_label(data),
            'timezone_label': _zone_label(displayed), 'slot_label': TIME_SLOTS[data['time_slot']],
            'usable': usable, 'source_available': source_available, 'status': status}


def _group(row):
    data, source = row['data'], row['data'].get('source', {})
    return (data['source_kind'], data.get('source_label', ''),
            source.get('report_context', {}).get('specimen_raw', ''),
            source.get('field_sources', {}).get('method_raw', {}).get('effective_value', ''),
            row['display_time']['timezone'], row['display_time']['utc_offset'], data['normalized_unit'])


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
                local = datetime.fromisoformat(row['display_time']['local_time'])
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
