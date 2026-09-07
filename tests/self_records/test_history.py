from uuid import uuid4

import pytest

from apps.self_records.history import history_series
from apps.self_records.services import create_record
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload


@pytest.mark.django_db
def test_history_uses_actual_time_independent_units_and_keeps_duplicates(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'daily-graph')
    records = []
    for minute, value in [('00:00', '50'), ('01:00', '60'), ('03:00', '70'), ('03:00', '71')]:
        records.append(create_record(patient, actor, payload(measured_local='2026-09-08T' + minute, value=value), creation_key=uuid4()).record)
    records.append(create_record(patient, actor, payload(kind='TEMPERATURE', unit='°F', value='98.6'), creation_key=uuid4()).record)
    records.append(create_record(patient, actor, payload(kind='SYMPTOM', symptom_name='乏力', severity='步行时明显'), creation_key=uuid4()).record)
    charts = {chart['kind']: chart for chart in history_series(records, 'Asia/Shanghai')}
    weight, temperature = charts['WEIGHT'], charts['TEMPERATURE']
    points = weight['points']
    assert len(points) == 4 and points[2]['x'] == points[3]['x']
    assert (points[1]['x'] - points[0]['x']) / (points[2]['x'] - points[0]['x']) == pytest.approx(1 / 3)
    assert weight['unit'] == 'kg' and temperature['unit'] == '°C'
    assert temperature['points'][0]['value'] == '37'
    assert charts['SYMPTOM']['points'][0]['label'] == '乏力 · 步行时明显'
    assert sum(len(chart['points']) for chart in charts.values()) == len(records)
