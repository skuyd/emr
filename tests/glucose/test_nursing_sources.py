from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import PermissionDenied
import pytest

from apps.glucose.models import GlucoseRecord
from apps.glucose.payloads import GlucoseInputError
from apps.glucose.services import GlucoseConflict, import_nursing_record, revise_record
from apps.glucose.sources import preview_nursing_page, source_current
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def nursing_case(django_user_model):
    _, patient = _patient(django_user_model, 'nursing-glucose')
    document, pages = _document(patient, page_count=1)
    data = {'value': '7.20', 'unit': 'mmol/L', 'measured_local': '2026-08-02T08:02', 'time_precision': 'MINUTE',
            'timezone': 'Asia/Shanghai', 'time_slot': 'FASTING', 'source_label': '末梢血糖', 'notes': ''}
    return patient, document, pages[0], data


def transcribe(patient, page, data, **options):
    preview = preview_nursing_page(patient, patient.account, page.pk)
    return import_nursing_record(patient, patient.account, page.pk, data,
        expected_source=preview['source_fingerprint'], checked_original=options.pop('checked_original', True),
        creation_key=options.pop('creation_key', uuid4()), original_excerpt='合成护理测量记录',
        measurement_scope=options.pop('measurement_scope', 'SINGLE'), **options)


def test_manual_nursing_transcription_binds_real_unparsed_page_and_actual_actor(django_user_model):
    patient, document, page, data = nursing_case(django_user_model)
    record = transcribe(patient, page, data, confirm_timezone=True).record
    assert record.source_document_id == document.pk and record.source_page_id == page.pk
    assert record.source_observation_id is None and record.source_parsing_version_id is None
    assert record.source_kind == 'NURSING' and record.created_by_id == patient.account_id
    assert record.current_data['field_origins']['value'] == 'USER_TRANSCRIBED'
    assert record.current_data['source']['original_excerpt'] == '合成护理测量记录'
    assert record.current_data['plot_eligible'] and source_current(record)


def test_nursing_zone_requires_separate_confirmation_and_does_not_claim_source_print(django_user_model):
    patient, _, page, data = nursing_case(django_user_model)
    data['timezone_origin'] = 'SOURCE_EXPLICIT'
    record = transcribe(patient, page, data).record
    assert record.measured_at is None and record.current_data['timezone_origin'] == 'UNCONFIRMED'


def test_nursing_summary_stays_off_the_curve_even_when_value_and_time_are_corrected(django_user_model):
    patient, _, page, data = nursing_case(django_user_model)
    data['value'] = '7.0–9.0'
    record = transcribe(patient, page, data, measurement_scope='SUMMARY', confirm_timezone=True).record
    assert not record.current_data['plot_eligible']
    data['value'] = '7.2'
    data['timezone_origin'] = 'USER_CONFIRMED'
    revised = revise_record(patient, patient.account, record.pk, action='CORRECT', expected_revision=0, changes=data)
    assert not revised.current_data['plot_eligible']
    assert revised.current_data['source']['measurement_scope'] == 'SUMMARY'


@pytest.mark.parametrize('scope', ['PLAN', '', 'DOSE', None])
def test_a_future_monitoring_plan_or_drug_dose_is_not_a_nursing_measurement(django_user_model, scope):
    patient, _, page, data = nursing_case(django_user_model)
    with pytest.raises(GlucoseInputError):
        transcribe(patient, page, data, measurement_scope=scope)
    assert not GlucoseRecord.objects.exists()


def test_nursing_creation_replay_does_not_overwrite_a_correction(django_user_model):
    patient, _, page, data = nursing_case(django_user_model)
    key = uuid4()
    record = transcribe(patient, page, data, creation_key=key).record
    correction = deepcopy(data)
    correction['value'] = '7.4'
    revised = revise_record(patient, patient.account, record.pk, action='CORRECT', expected_revision=0, changes=correction)
    replay = transcribe(patient, page, data, creation_key=key)
    assert not replay.created and replay.record.pk == revised.pk
    assert replay.record.current_data['raw_value'] == '7.4'


def test_nursing_stale_page_replay_rejects_before_returning_existing_record(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document

    patient, document, page, data = nursing_case(django_user_model)
    key = uuid4()
    previous = preview_nursing_page(patient, patient.account, page.pk)
    record = transcribe(patient, page, data, creation_key=key).record
    move_to_trash(patient, document.pk, actor=patient.account)
    restore_document(patient, document.pk, actor=patient.account)
    assert not source_current(record)
    with pytest.raises(GlucoseConflict):
        import_nursing_record(patient, patient.account, page.pk, data,
            expected_source=previous['source_fingerprint'], checked_original=True, creation_key=key,
            original_excerpt='合成护理测量记录', measurement_scope='SINGLE')


def test_nursing_source_must_belong_to_current_patient(django_user_model):
    patient, _, page, _ = nursing_case(django_user_model)
    _, other = _patient(django_user_model, 'other-nursing')
    with pytest.raises(PermissionDenied):
        preview_nursing_page(other, other.account, page.pk)


def test_nursing_recheck_after_reparse_keeps_uuid_and_first_transcription(django_user_model):
    from tests.facts.factories import parsed_facts

    patient, document, page, data = nursing_case(django_user_model)
    record = transcribe(patient, page, data).record
    original = deepcopy(record.original_data)
    _, version = parsed_facts(patient, ['合成护理记录', '血糖测量已核对'], document=document)
    assert not source_current(record)
    data['value'] = '7.4'
    rechecked = transcribe(patient, page, data, recheck_record_id=record.pk, expected_revision=0).record
    assert rechecked.pk == record.pk and rechecked.source_parsing_version_id == version.pk
    assert rechecked.original_data == original and rechecked.current_data['raw_value'] == '7.4'
    assert rechecked.revisions.get().action == 'RECHECK' and source_current(rechecked)
