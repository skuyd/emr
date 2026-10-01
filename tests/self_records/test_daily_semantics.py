import pytest

from apps.self_records.forms import RecordForm
from apps.self_records.payloads import InvalidRecord, normalize_payload


def test_entered_local_minute_is_kept_without_timezone_or_dst_conversion():
    result = normalize_payload({
        'kind': 'WEIGHT', 'measured_local': '2026-10-25T02:30', 'value': '60.0', 'unit': 'kg',
    })
    assert result['local_time'] == '2026-10-25T02:30'
    assert result['record_date'] == '2026-10-25'
    assert result['record_time'] == '02:30'
    assert result['time_precision'] == 'MINUTE'
    assert 'timezone' not in result and 'measured_at' not in result


@pytest.mark.parametrize('raw', ['', '2026-10-25', '2026-02-30T08:25', '2026-10-25T08:25:30',
                                 '2026-10-25T08:25+02:00'])
def test_timed_record_rejects_missing_or_invalid_local_minute(raw):
    with pytest.raises(InvalidRecord) as error:
        normalize_payload({'kind': 'WEIGHT', 'measured_local': raw, 'value': '60', 'unit': 'kg'})
    assert error.value.field == 'measured_local'


@pytest.mark.parametrize('score', [0, 5])
def test_ecog_keeps_only_date_and_explicit_selected_grade(score):
    result = normalize_payload({'kind': 'ECOG', 'record_date': '2026-10-01', 'score': score})
    assert result['local_time'] == '2026-10-01'
    assert result['record_date'] == '2026-10-01'
    assert result['record_time'] is None
    assert result['time_precision'] == 'DATE'
    assert result['score'] == score
    assert 'measured_at' not in result and 'timezone' not in result


@pytest.mark.parametrize('score', [None, '', -1, 6, 1.5, True, 'zero'])
def test_ecog_rejects_unselected_or_invalid_grade(score):
    with pytest.raises(InvalidRecord) as error:
        normalize_payload({'kind': 'ECOG', 'record_date': '2026-10-01', 'score': score})
    assert error.value.field == 'score'


def test_symptom_requires_name_after_trimming_whitespace():
    with pytest.raises(InvalidRecord) as error:
        normalize_payload({'kind': 'SYMPTOM', 'measured_local': '2026-10-01T13:45', 'symptom_name': '   '})
    assert error.value.field == 'symptom_name'


def test_ecog_form_requires_selected_grade_without_time_or_timezone():
    initial = RecordForm(kind='ECOG')
    assert 'score' in initial.fields and initial['score'].value() is None
    assert 'measured_local' not in initial.fields
    assert 'timezone' not in initial.fields
    data = {'kind': 'ECOG', 'record_date': '2026-10-01', 'score': '0',
            'creation_key': '937ac048-96df-44f5-9c58-f80d06408779'}
    assert RecordForm(data, kind='ECOG').is_valid()
    data['score'] = ''
    assert 'score' in RecordForm(data, kind='ECOG').errors
