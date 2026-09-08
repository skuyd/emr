from types import SimpleNamespace
from uuid import uuid4

from apps.glucose.forms import GlucoseRecordForm, HistoryFilterForm, LabImportForm, NursingImportForm
from apps.glucose.payloads import normalize_payload


def values(**changes):
    return {'source_kind': 'METER', 'creation_key': str(uuid4()), 'value': ' ７.２ ', 'unit': 'mmol/L',
            'measured_local': '2026-08-02T06:12:34', 'time_precision': 'SECOND',
            'timezone': 'Asia/Shanghai', 'time_slot': 'FASTING', 'source_label': '', 'notes': '', **changes}


def record(*, kind='LAB_REPORT', **changes):
    data = values(**changes)
    normalized = normalize_payload(data, source_kind=kind, allow_imprecise=kind in ('LAB_REPORT', 'NURSING'))
    normalized['source'] = {}
    return SimpleNamespace(current_data=normalized, source_kind=kind, revision_number=4, creation_key=uuid4())


def test_manual_form_preserves_entered_value_and_seconds():
    form = GlucoseRecordForm(values())
    assert form.is_valid(), form.errors
    data = normalize_payload(form.payload(), source_kind=form.cleaned_data['source_kind'])
    assert data['raw_value'] == ' ７.２ ' and data['local_time'] == '2026-08-02T06:12:34'
    assert data['measured_at'] == '2026-08-01T22:12:34+00:00'


def test_manual_form_cannot_claim_a_report_source_or_invent_missing_time():
    assert not GlucoseRecordForm(values(source_kind='LAB_REPORT')).is_valid()
    assert not GlucoseRecordForm(values(time_precision='DAY', measured_local='2026-08-02')).is_valid()


def test_source_edit_does_not_confirm_timezone_just_because_textbox_contains_a_zone():
    original = record()
    form = GlucoseRecordForm(values(source_kind='LAB_REPORT', expected_revision=4), record=original)
    assert form.is_valid(), form.errors
    data = normalize_payload(form.payload(), source_kind='LAB_REPORT', allow_imprecise=True)
    assert data['timezone_origin'] == 'UNCONFIRMED' and data['timezone'] == ''
    assert data['measured_at'] is None


def test_source_edit_uses_user_confirmation_and_ignores_forged_origin_input():
    form = GlucoseRecordForm(values(source_kind='LAB_REPORT', expected_revision=4, confirm_timezone='on',
        timezone_origin='SOURCE_EXPLICIT'), record=record())
    assert form.is_valid(), form.errors
    assert form.payload()['timezone_origin'] == 'USER_CONFIRMED'


def test_nursing_form_has_no_automatic_today_timestamp_or_timezone():
    form = NursingImportForm(candidate={'source_fingerprint': 'a' * 64})
    assert form['measured_local'].value() == '' and form['timezone'].value() == ''
    assert form['time_precision'].value() == 'UNKNOWN'
    assert not form['confirm_timezone'].value() and not form['checked_original'].value()


def test_nursing_partial_date_and_raw_range_survive_without_a_midpoint():
    form = NursingImportForm(values(source_kind='NURSING', value='7.0–9.0', time_precision='MONTH',
        measured_local='2026-08', timezone='', expected_source='a' * 64,
        original_excerpt='本月血糖7.0–9.0 mmol/L', measurement_scope='SUMMARY', checked_original='on'),
        candidate={'source_fingerprint': 'a' * 64})
    assert form.is_valid(), form.errors
    data = normalize_payload(form.payload(), source_kind='NURSING', allow_imprecise=True)
    assert data['result_type'] == 'RANGE' and data['normalized_value'] is None
    assert data['time_precision'] == 'MONTH' and data['measured_at'] is None


def test_nursing_import_requires_original_excerpt_and_explicit_check():
    form = NursingImportForm(values(source_kind='NURSING', expected_source='a' * 64, measurement_scope='SINGLE'),
        candidate={'source_fingerprint': 'a' * 64})
    assert not form.is_valid()
    assert {'original_excerpt', 'checked_original'} <= set(form.errors)


def test_source_kind_is_immutable_in_edit_form():
    form = GlucoseRecordForm(values(source_kind='MANUAL', expected_revision=4), record=record())
    assert not form.is_valid() and 'source_kind' in form.errors


def test_existing_dst_fold_survives_unrelated_edit_but_changed_wall_time_needs_new_offset():
    original = record(kind='METER', measured_local='2026-11-01T01:30-04:00', time_precision='MINUTE',
                      timezone='America/New_York')
    form = GlucoseRecordForm(values(expected_revision=4, measured_local='2026-11-01T01:30',
        time_precision='MINUTE', timezone='America/New_York', notes='更正备注'), record=original)
    assert form.is_valid(), form.errors
    assert form.payload()['measured_local'] == '2026-11-01T01:30-04:00'
    changed = GlucoseRecordForm(values(expected_revision=4, measured_local='2026-11-01T01:31',
        time_precision='MINUTE', timezone='America/New_York'), record=original)
    assert not changed.is_valid() and 'measured_local' in changed.errors


def test_recheck_uses_new_operation_key_and_current_record_revision():
    original = record()
    lab = LabImportForm(candidate={'source_fingerprint': 'a' * 64}, record=original)
    assert str(lab['creation_key'].value()) != str(original.creation_key)
    assert lab['expected_revision'].value() == 4 and lab['expected_source'].value() == 'a' * 64
    nursing = NursingImportForm(candidate={'source_fingerprint': 'b' * 64}, record=record(kind='NURSING'))
    assert str(nursing['creation_key'].value()) != str(original.creation_key)


def test_lab_import_requires_explicit_original_check_and_valid_confirmed_zone():
    data = {'creation_key': str(uuid4()), 'expected_source': 'a' * 64, 'confirm_timezone': 'on', 'timezone': 'Invalid/Zone'}
    form = LabImportForm(data, candidate={'source_fingerprint': 'a' * 64})
    assert not form.is_valid() and {'checked_original', 'timezone'} <= set(form.errors)


def test_history_filters_reject_reversed_range_and_unknown_source_kind():
    assert not HistoryFilterForm({'start': '2026-09-01', 'end': '2026-08-01'}).is_valid()
    assert not HistoryFilterForm({'source_kind': 'OTHER'}).is_valid()
    assert HistoryFilterForm({'date_scope': 'UNKNOWN', 'time_slot': 'UNSPECIFIED'}).is_valid()
