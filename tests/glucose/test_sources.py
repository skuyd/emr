from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import PermissionDenied
import pytest

from apps.glucose.models import GlucoseRecord
from apps.glucose.payloads import GlucoseInputError
from apps.glucose.services import GlucoseConflict, import_lab_record, revise_record
from apps.glucose.sources import GlucoseSourceUnavailable, preview_lab, source_current
from apps.labs.revisions import revise_observation
from tests.glucose.factories import lab_source


pytestmark = pytest.mark.django_db


def import_source(patient, observation, **options):
    preview = preview_lab(patient, patient.account, observation.pk)
    return import_lab_record(patient, patient.account, observation.pk,
        expected_source=preview['source_fingerprint'], creation_key=options.pop('creation_key', uuid4()),
        checked_original=options.pop('checked_original', True), **options)


def test_preview_preserves_actual_sample_seconds_separate_report_time_and_raw_field_sources(django_user_model):
    _, patient, document, version, row = lab_source(django_user_model)
    preview = preview_lab(patient, patient.account, row.pk)
    data = preview['data']
    assert data['raw_value'] == '8.20' and data['raw_unit'] == 'mmol/L'
    assert data['measured_local_raw'] == '2026-08-02T06:12:34' and data['time_precision'] == 'SECOND'
    assert data['measured_at'] is None and data['timezone_origin'] == 'UNCONFIRMED'
    assert not data['plot_eligible'] and data['time_slot'] == 'UNSPECIFIED'
    assert data['source']['report_context']['report_time']['local'] == '2026-08-02T09:24:56'
    source = data['source']['field_sources']['raw_value']
    assert source['observation_id'] == str(row.pk) and source['evidence_id'] == str(row.evidence_id)
    assert source['parsing_version_id'] == str(version.pk) and source['document_id'] == str(document.pk)
    assert source['original_text'] == row.evidence.source_text
    assert GlucoseRecord.objects.count() == 0


@pytest.mark.parametrize('checked', [False, 'false', 'true', 1, None])
def test_source_import_requires_actual_explicit_original_check(django_user_model, checked):
    _, patient, _, _, row = lab_source(django_user_model)
    with pytest.raises(GlucoseInputError):
        import_source(patient, row, checked_original=checked)
    assert not GlucoseRecord.objects.exists()


def test_original_import_is_independent_and_timezone_confirmation_is_user_provenance(django_user_model):
    _, patient, document, version, row = lab_source(django_user_model)
    created = import_source(patient, row, timezone_name='Asia/Shanghai', confirm_timezone=True)
    record = created.record
    assert created.created and record.source_document_id == document.pk
    assert record.source_observation_id == row.pk and record.source_parsing_version_id == version.pk
    assert record.source_page_id == row.document_page_id and record.created_by_id == patient.account_id
    assert record.current_data['measured_at'] == '2026-08-01T22:12:34+00:00'
    assert record.current_data['timezone_origin'] == 'USER_CONFIRMED'
    assert record.current_data['field_origins']['timezone'] == 'USER_CONFIRMED'
    assert record.current_data['source']['report_context']['sample_time']['timezone_origin'] == 'UNCONFIRMED'
    assert source_current(record)


def test_timezone_name_without_explicit_confirmation_does_not_assign_a_zone(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    record = import_source(patient, row, timezone_name='Asia/Shanghai').record
    assert record.measured_at is None and record.current_data['timezone'] == ''


@pytest.mark.parametrize('raw_name,specimen', [('GLU', 'URINE'), ('糖化血红蛋白', 'BLOOD'),
                                            ('葡萄糖注射液', 'BLOOD'), ('葡萄糖', '')])
def test_dictionary_code_and_unit_do_not_turn_urine_hba1c_or_drug_into_blood_glucose(django_user_model, raw_name, specimen):
    _, patient, _, _, row = lab_source(django_user_model, raw_name=raw_name, specimen=specimen)
    with pytest.raises(GlucoseSourceUnavailable):
        preview_lab(patient, patient.account, row.pk)
    assert not GlucoseRecord.objects.exists()


def test_one_current_observation_cannot_be_imported_twice(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    first = import_source(patient, row)
    again = import_source(patient, row)
    assert first.created and not again.created and first.record.pk == again.record.pk
    assert GlucoseRecord.objects.count() == 1


def test_stale_source_token_is_rejected_before_creation_key_replay(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    key = uuid4()
    preview = preview_lab(patient, patient.account, row.pk)
    record = import_source(patient, row, creation_key=key).record
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '8.40'}, expected_revision=0)
    with pytest.raises(GlucoseConflict):
        import_lab_record(patient, patient.account, row.pk, expected_source=preview['source_fingerprint'],
                          creation_key=key, checked_original=True)
    assert not source_current(record) and record.original_data['raw_value'] == '8.20'


def test_changed_source_recheck_appends_to_same_uuid_and_never_rewrites_first_snapshot(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    record = import_source(patient, row).record
    original = deepcopy(record.original_data)
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '8.40'}, expected_revision=0)
    with pytest.raises(GlucoseConflict):
        import_source(patient, row)
    checked = import_source(patient, row, recheck=True, expected_revision=0).record
    assert checked.pk == record.pk and checked.revision_number == 1
    assert checked.original_data == original and checked.current_data['raw_value'] == '8.40'
    assert checked.revisions.get().action == 'RECHECK' and source_current(checked)


def test_source_revision_invalidates_correction_and_undo_cannot_restore_confirmation(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    record = import_source(patient, row).record
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '8.40'}, expected_revision=0)
    with pytest.raises(GlucoseConflict):
        revise_record(patient, patient.account, record.pk, action='CORRECT', expected_revision=0, changes={})
    checked = import_source(patient, row, recheck=True, expected_revision=0).record
    undone = revise_record(patient, patient.account, checked.pk, action='UNDO', expected_revision=1)
    assert undone.current_data['raw_value'] == '8.20' and not source_current(undone)


def test_new_parsing_observation_is_a_new_explicit_import_and_old_record_stays_stale(django_user_model):
    _, patient, document, version, row = lab_source(django_user_model)
    first = import_source(patient, row).record
    _, _, _, _, new_row = lab_source(django_user_model, patient=patient, document=document, previous=version)
    second = import_source(patient, new_row).record
    assert first.pk != second.pk and not source_current(first) and source_current(second)
    with pytest.raises(GlucoseSourceUnavailable):
        preview_lab(patient, patient.account, row.pk)


def test_effective_date_correction_does_not_splice_original_clock_onto_another_day(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'observation_date': '2026-08-04'}, expected_revision=0)
    data = preview_lab(patient, patient.account, row.pk)['data']
    assert data['measured_local_raw'] == '2026-08-04' and data['time_precision'] == 'DAY'
    assert data['measured_at'] is None
    assert data['source']['report_context']['sample_time']['local'] == '2026-08-02T06:12:34'


def test_authorization_uses_actual_patient_and_account(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    _, other, _, _, _ = lab_source(django_user_model, marker='other-glucose-source')
    with pytest.raises(PermissionDenied):
        preview_lab(other, other.account, row.pk)
    with pytest.raises(PermissionDenied):
        preview_lab(patient, other.account, row.pk)


def test_source_delete_restore_does_not_reactivate_old_confirmation(django_user_model):
    from apps.documents.lifecycle import restore_document, move_to_trash

    _, patient, document, _, row = lab_source(django_user_model)
    record = import_source(patient, row).record
    move_to_trash(patient, document.pk, actor=patient.account)
    assert not source_current(record)
    restore_document(patient, document.pk, actor=patient.account)
    assert not source_current(record)
    checked = import_source(patient, row, recheck=True, expected_revision=0).record
    assert checked.pk == record.pk and source_current(checked)


def test_inherited_fields_keep_the_old_origin_page_and_revision_instead_of_new_parse(django_user_model):
    _, patient, document, old_version, old_row = lab_source(django_user_model)
    revise_observation(patient.account, old_row.pk, action='CORRECT', changes={'raw_value': '8.40'}, expected_revision=0)
    _, _, _, new_version, new_row = lab_source(django_user_model, patient=patient, document=document, previous=old_version)
    data = preview_lab(patient, patient.account, new_row.pk)['data']
    assert data['raw_value'] == '8.40' and data['source']['parsing_version_id'] == str(new_version.pk)
    field = data['source']['field_sources']['raw_value']
    assert field['observation_id'] == str(old_row.pk) and field['parsing_version_id'] == str(old_version.pk)
    assert field['revision_id'] == str(old_row.revisions.get().pk)
    assert data['source']['report_context']['sample_time']['evidence'][0]['block_id'] == str(old_version.ocr_blocks.get(reading_order=3).pk)


def test_changing_inherited_source_revision_also_changes_new_preview_token(django_user_model):
    _, patient, document, old_version, old_row = lab_source(django_user_model)
    revise_observation(patient.account, old_row.pk, action='CORRECT', changes={'raw_value': '8.40'}, expected_revision=0)
    _, _, _, _, row = lab_source(django_user_model, patient=patient, document=document, previous=old_version)
    first = preview_lab(patient, patient.account, row.pk)
    # A later active revision selecting automatic data replaces the carried fields.
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_unit': 'mg/dL'}, expected_revision=0)
    second = preview_lab(patient, patient.account, row.pk)
    assert first['source_fingerprint'] != second['source_fingerprint']
    assert second['data']['source']['field_sources']['raw_value']['observation_id'] == str(old_row.pk)
    assert second['data']['source']['field_sources']['raw_unit']['observation_id'] == str(row.pk)


def test_a_user_time_correction_cannot_claim_the_report_printed_the_timezone(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    record = import_source(patient, row).record
    corrected = revise_record(patient, patient.account, record.pk, action='CORRECT', expected_revision=0, changes={
        'value': '8.20', 'unit': 'mmol/L', 'measured_local': '2026-08-02T06:12:34', 'time_precision': 'SECOND',
        'timezone': 'Asia/Shanghai', 'timezone_origin': 'SOURCE_EXPLICIT', 'time_slot': 'UNSPECIFIED',
    })
    assert corrected.current_data['timezone_origin'] == 'USER_CONFIRMED'
    assert corrected.current_data['source']['report_context']['sample_time']['timezone_origin'] == 'UNCONFIRMED'


def test_reactivated_parsing_version_does_not_restore_an_old_source_token(django_user_model):
    from apps.processing.models import ParsingVersion

    _, patient, document, old_version, row = lab_source(django_user_model)
    first = import_source(patient, row).record
    lab_source(django_user_model, patient=patient, document=document, previous=old_version)
    ParsingVersion.objects.activate(old_version)
    assert not source_current(first)


def test_offset_is_required_for_an_ambiguous_user_confirmed_source_time(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model, sample='2026-11-01 01:30:12')
    with pytest.raises(GlucoseInputError):
        import_source(patient, row, timezone_name='America/New_York', confirm_timezone=True)
    record = import_source(patient, row, timezone_name='America/New_York', confirm_timezone=True, utc_offset='-05:00').record
    assert record.current_data['measured_at'] == '2026-11-01T06:30:12+00:00'


def test_fasting_from_a_lab_name_correction_retains_the_revision_origin(django_user_model):
    _, patient, _, _, row = lab_source(django_user_model)
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_name': '空腹血糖'}, expected_revision=0)
    data = preview_lab(patient, patient.account, row.pk)['data']
    assert data['time_slot'] == 'FASTING'
    assert data['field_origins']['time_slot'] == 'LAB_REVISION'
