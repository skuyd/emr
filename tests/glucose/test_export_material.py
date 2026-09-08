from copy import deepcopy
import json
from uuid import uuid4

from django.core.exceptions import PermissionDenied
import pytest

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.glucose.exporting import assert_material_current, selected_material
from apps.glucose.services import create_record, revise_record
from apps.labs.revisions import revise_observation
from tests.glucose.factories import lab_source
from tests.glucose.test_payloads import payload
from tests.glucose.test_sources import import_source
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def snapshot(selection, material):
    return {'selection': selection, 'glucose_records': material['records'],
            'glucose_record_sources': material['sources'], 'glucose_fingerprint': material['fingerprint'],
            'glucose_document_ids': material['document_ids']}


def test_only_selected_current_values_are_exported_with_raw_conversion_and_real_author(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-export-material')
    chosen = create_record(patient, actor, payload(value='100', unit='mg/dL'), creation_key=uuid4(), source_kind='METER').record
    other = create_record(patient, actor, payload(value='321', unit='mg/dL'), creation_key=uuid4()).record
    selection = {'glucose_record_ids': [str(chosen.pk)]}
    material = selected_material(patient, selection, lock=True)
    assert [row['id'] for row in material['records']] == [str(chosen.pk)]
    row = material['records'][0]
    assert row['created_by'] == str(actor.pk) and row['revision_number'] == 0
    assert row['data']['raw_value'] == '100' and row['data']['normalized_value'] == '5.551'
    assert row['data']['conversion']['factor'] == '0.05551'
    assert material['document_ids'] == []
    assert material['sources'][0]['source_kind'] == 'METER'
    assert str(other.pk) not in json.dumps(material)
    assert_material_current(patient, snapshot(selection, material))


def test_report_dependency_is_retained_without_exporting_its_unselected_ocr_or_context(django_user_model):
    _, patient, document, version, observation = lab_source(django_user_model)
    context = version.ocr_blocks.get(reading_order=0)
    context.text += ' 未选择的诊断文字'
    context.save(update_fields=['text'])
    observation.field_evidence['raw_value']['source_text'] = '未选择的定位附文'
    observation.save(update_fields=['field_evidence'])
    record = import_source(patient, observation).record
    material = selected_material(patient, {'glucose_record_ids': [str(record.pk)]}, lock=True)
    assert material['document_ids'] == [str(document.pk)]
    source = material['sources'][0]
    assert source['document_id'] == str(document.pk) and source['page_id'] == str(record.source_page_id)
    assert source['observation_id'] == str(observation.pk)
    assert source['sampling']['local'] == '2026-08-02T06:12:34'
    assert source['reporting']['local'] == '2026-08-02T09:24:56'
    assert source['sampling']['timezone_origin'] == 'UNCONFIRMED'
    assert '未选择的诊断文字' not in json.dumps(material, ensure_ascii=False)
    assert '未选择的定位附文' not in json.dumps(material, ensure_ascii=False)
    assert 'source' not in material['records'][0]['data']
    assert 'source' not in material['records'][0]['original_data']


def test_record_correction_and_undo_never_restore_an_old_export_fingerprint(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-export-revision')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    selection = {'glucose_record_ids': [str(record.pk)]}
    first = snapshot(selection, selected_material(patient, selection, lock=True))
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='6.80'))
    with pytest.raises(SnapshotChanged):
        assert_material_current(patient, first)
    second = selected_material(patient, selection, lock=True)
    assert second['records'][0]['original_data']['raw_value'] == '5.50'
    assert second['records'][0]['data']['raw_value'] == '6.80'
    assert second['records'][0]['revision_id']
    revise_record(patient, actor, record.pk, action='UNDO', expected_revision=1)
    with pytest.raises(SnapshotChanged):
        assert_material_current(patient, first)


def test_source_change_rejects_a_record_even_when_its_current_record_values_did_not_change(django_user_model):
    _, patient, _, _, observation = lab_source(django_user_model)
    record = import_source(patient, observation).record
    selection = {'glucose_record_ids': [str(record.pk)]}
    first = snapshot(selection, selected_material(patient, selection, lock=True))
    revise_observation(patient.account, observation.pk, action='CORRECT', expected_revision=0, changes={'raw_value': '9.30'})
    with pytest.raises(ExportInputError):
        selected_material(patient, selection, lock=True)
    with pytest.raises(SnapshotChanged):
        assert_material_current(patient, first)


def test_missing_foreign_or_deleted_records_cannot_silently_shrink_selection(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-export-owned')
    _, other, _, other_actor, _ = family(django_user_model, 'glucose-export-foreign')
    record = create_record(other, other_actor, payload(), creation_key=uuid4()).record
    for identity in (record.pk, uuid4()):
        with pytest.raises(PermissionDenied):
            selected_material(patient, {'glucose_record_ids': [str(identity)]}, lock=True)
    own = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, own.pk, action='DELETE', expected_revision=0)
    with pytest.raises(ExportInputError):
        selected_material(patient, {'glucose_record_ids': [str(own.pk)]}, lock=True)


def test_unselected_record_changes_do_not_invalidate_selected_record_material(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-export-unselected')
    chosen = create_record(patient, actor, payload(), creation_key=uuid4()).record
    other = create_record(patient, actor, payload(), creation_key=uuid4()).record
    selection = {'glucose_record_ids': [str(chosen.pk)]}
    first = snapshot(selection, selected_material(patient, selection, lock=True))
    revise_record(patient, actor, other.pk, action='DELETE', expected_revision=0)
    assert_material_current(patient, first)


def test_old_snapshot_without_glucose_is_compatible_but_missing_selected_table_is_not(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-export-compat')
    assert_material_current(patient, {'selection': {}})
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    selection = {'glucose_record_ids': [str(record.pk)]}
    valid = snapshot(selection, selected_material(patient, selection, lock=True))
    for key in ('glucose_records', 'glucose_record_sources', 'glucose_fingerprint'):
        altered = deepcopy(valid)
        altered.pop(key)
        with pytest.raises(SnapshotChanged):
            assert_material_current(patient, altered)


def test_actual_contributor_purge_invalidates_author_metadata_without_deleting_family_values(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion, AccountDeletionOutcome
    from apps.accounts.models import AccountDeletionJob
    _, patient, _, actor, _ = family(django_user_model, 'glucose-export-author-purge')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='6.70'))
    selection = {'glucose_record_ids': [str(record.pk)]}
    first = snapshot(selection, selected_material(patient, selection, lock=True))
    request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    job = AccountDeletionJob.objects.get(account_id=actor.pk)
    assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
    with pytest.raises(SnapshotChanged):
        assert_material_current(patient, first)
    current = selected_material(patient, selection, lock=True)['records'][0]
    assert current['data']['raw_value'] == '6.70'
    assert current['created_by'] is None and current['updated_by'] is None and current['revision_author'] is None


def test_unparsed_nursing_page_retains_real_dependency_and_transcription_origin(django_user_model):
    from tests.glucose.test_nursing_sources import nursing_case, transcribe
    patient, document, page, data = nursing_case(django_user_model)
    record = transcribe(patient, page, data).record
    selection = {'glucose_record_ids': [str(record.pk)]}
    material = selected_material(patient, selection, lock=True)
    assert material['document_ids'] == [str(document.pk)]
    assert material['sources'][0]['page_id'] == str(page.pk)
    assert material['sources'][0]['parsing_version_id'] is None
    assert material['records'][0]['data']['field_origins']['value'] == 'USER_TRANSCRIBED'
    assert_material_current(patient, snapshot(selection, material))
