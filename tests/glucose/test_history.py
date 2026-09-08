from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from apps.glucose.history import display_row, history_charts
from apps.glucose.payloads import normalize_payload


def row(value='7.2', *, measured='2026-08-02T08:30', unit='mmol/L', zone='Asia/Shanghai',
        slot='FASTING', source_kind='MANUAL', source_available=True, deleted=False, scope=None):
    data = normalize_payload({'value': value, 'unit': unit, 'measured_local': measured,
        'time_precision': 'MINUTE' if measured else 'UNKNOWN', 'timezone': zone,
        'timezone_origin': 'UNCONFIRMED' if not zone else 'USER_CONFIRMED', 'time_slot': slot,
        'source_label': '合成设备', 'notes': ''}, source_kind=source_kind,
        allow_imprecise=source_kind in ('LAB_REPORT', 'NURSING'))
    data['source'] = {'measurement_scope': scope} if scope else {}
    record = SimpleNamespace(pk=uuid4(), current_data=data, source_kind=source_kind, deleted_at=True if deleted else None)
    return display_row(record, source_available=source_available)


def test_intraday_points_use_actual_minute_and_preserve_duplicate_values_without_averaging():
    rows = [row('6.1', measured='2026-08-02T07:00'), row('7.2'), row('8.4'), row('6.7', measured='2026-08-02T10:00')]
    result = history_charts(rows)
    chart = result['charts'][0]
    assert len(chart['series']) == 1 and len(chart['series'][0]['points']) == 4
    points = chart['series'][0]['points']
    same_time = [point for point in points if point['seconds'] == 8 * 3600 + 30 * 60]
    assert {point['value'] for point in same_time} == {'7.2', '8.4'}
    assert all(point['same_time_count'] == 2 for point in same_time)
    assert chart['series'][0]['segments'] == []
    assert [point['seconds'] for point in points] == [25200, 30600, 30600, 36000]


def test_multiday_overlay_uses_separate_days_within_one_comparable_source():
    result = history_charts([row('7.1'), row('7.3', measured='2026-08-03T08:30')])
    assert len(result['charts']) == 1
    assert [series['day'] for series in result['charts'][0]['series']] == ['2026-08-02', '2026-08-03']


def test_heatmap_keeps_multiple_readings_in_explicit_slot_and_does_not_guess_from_clock():
    result = history_charts([row('7.1'), row('7.3'), row('8.0', slot='UNSPECIFIED')])
    heatmap = result['heatmaps'][0]
    cells = {cell['slot']: cell for cell in heatmap['days'][0]['cells']}
    assert len(cells['FASTING']['rows']) == 2
    assert len(cells['UNSPECIFIED']['rows']) == 1
    assert cells['AT_0300']['rows'] == []
    assert {item['data']['raw_value'] for item in cells['FASTING']['rows']} == {'7.1', '7.3'}


def test_missing_time_zone_and_ranges_are_retained_outside_intraday_points():
    rows = [row('7.0', measured='', zone='', source_kind='NURSING'),
            row('7.0–9.0', source_kind='NURSING'), row('6.1', zone='', source_kind='LAB_REPORT')]
    result = history_charts(rows)
    assert result['charts'] == [] and len(result['omitted']) == 3
    assert sum(len(cell['rows']) for heatmap in result['heatmaps'] for day in heatmap['days'] for cell in day['cells']) == 3
    assert any(day['day'] == '日期不详' for heatmap in result['heatmaps'] for day in heatmap['days'])


def test_stale_and_deleted_source_values_do_not_enter_heatmaps_or_curves():
    rows = [row('7.1', source_available=False), row('7.3', deleted=True)]
    result = history_charts(rows)
    assert result['charts'] == result['heatmaps'] == []
    assert len(result['omitted']) == 2
    assert not rows[0]['usable'] and not rows[1]['usable']


def test_serum_capillary_device_timezone_and_offset_groups_are_separate():
    rows = [row(source_kind='LAB_REPORT'), row(source_kind='NURSING'), row(source_kind='MANUAL'),
            row(source_kind='METER'), row(zone='UTC')]
    result = history_charts(rows)
    assert len(result['charts']) == 5 and len(result['heatmaps']) == 5


def test_nursing_summary_cannot_become_a_point_or_relative_color_by_value_correction():
    original = row('7.2', source_kind='NURSING', scope='SUMMARY')
    data = deepcopy(original['data'])
    result = history_charts([original])
    assert result['charts'] == []
    entry = result['heatmaps'][0]['days'][0]['cells'][0]['rows'][0]
    assert entry['shade'] is None
    assert original['data'] == data


def test_supported_extreme_values_and_zero_are_not_clipped_to_a_medical_range():
    result = history_charts([row('0'), row('1e30', measured='2026-08-02T09:00')])
    chart = result['charts'][0]
    assert chart['low'] == '0' and chart['high'] == '1E+30'
    assert len(chart['series'][0]['points']) == 2
    assert all(20 <= point['y'] <= 180 for point in chart['series'][0]['points'])
