from copy import deepcopy
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.documents.models import Document
from apps.glucose.models import GlucoseRecord
from apps.glucose.payloads import GlucoseInputError
from apps.glucose.services import GlucoseConflict, create_record, revise_record
from tests.glucose.test_payloads import payload
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def test_new_meter_readings_keep_actual_author_raw_conversion_and_individual_identity(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-create')
    key = uuid4()
    first = create_record(patient, actor, payload(value='100', unit='mg/dL'), creation_key=key, source_kind='METER')
    second = create_record(patient, actor, payload(value='100', unit='mg/dL'), creation_key=uuid4(), source_kind='METER')
    assert first.created and second.created and first.record.pk != second.record.pk
    record = first.record
    assert record.created_by_id == actor.pk and record.patient_id == patient.pk
    assert record.original_data['raw_value'] == '100' and record.original_data['normalized_value'] == '5.551'
    assert record.current_data['source_kind'] == 'METER' and record.source_document_id is None
    assert record.current_data['field_origins']['value'] == 'USER_ENTERED'
    assert record.measured_at == second.record.measured_at
    assert record.time_precision == 'MINUTE' and record.measured_date.isoformat() == '2026-09-08'
    assert not Document.objects.filter(patient=patient).exists()


def test_creation_retry_does_not_overwrite_a_correction_or_restore_a_deleted_record(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-retry')
    key = uuid4()
    record = create_record(patient, actor, payload(), creation_key=key).record
    original = deepcopy(record.original_data)
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='6.7'))
    revise_record(patient, actor, record.pk, action='DELETE', expected_revision=1)
    replay = create_record(patient, actor, payload(), creation_key=key)
    assert replay.record.pk == record.pk and not replay.created
    assert replay.record.original_data == original and replay.record.current_data['raw_value'] == '6.7'
    assert replay.record.deleted_at is not None and replay.record.revision_number == 2
    with pytest.raises(GlucoseConflict):
        create_record(patient, actor, payload(value='6.8'), creation_key=key)
    assert GlucoseRecord.objects.filter(patient=patient).count() == 1


def test_correct_delete_undo_keep_each_before_after_and_source_kind(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-history')
    record = create_record(patient, actor, payload(), creation_key=uuid4(), source_kind='METER').record
    original = deepcopy(record.original_data)
    correction = revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0,
                               changes=payload(value='HI', time_slot='FASTING', source_kind='LAB_REPORT'))
    assert correction.current_data['raw_value'] == 'HI' and not correction.current_data['plot_eligible']
    assert correction.current_data['source_kind'] == 'METER'
    assert correction.current_data['field_origins']['value'] == 'USER_CORRECTED'
    assert correction.current_data['field_origins']['time'] == 'USER_ENTERED'
    revise_record(patient, patient.account, record.pk, action='DELETE', expected_revision=1)
    restored = revise_record(patient, actor, record.pk, action='UNDO', expected_revision=2)
    assert restored.deleted_at is None and restored.current_data['raw_value'] == 'HI'
    assert restored.original_data == original
    history = list(restored.revisions.all())
    assert [revision.action for revision in history] == ['CORRECT', 'DELETE', 'UNDO']
    assert [revision.author_id for revision in history] == [actor.pk, patient.account_id, actor.pk]
    assert history[0].before['data']['raw_value'] == '5.50'
    assert history[0].after['data']['raw_value'] == 'HI'


@pytest.mark.parametrize('revision', [True, -1, '0', 1, None])
def test_only_the_current_integer_revision_can_mutate_a_record(django_user_model, revision):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-version')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    with pytest.raises(GlucoseConflict):
        revise_record(patient, actor, record.pk, action='DELETE', expected_revision=revision)
    record.refresh_from_db()
    assert record.revision_number == 0 and record.deleted_at is None and not record.revisions.exists()


@pytest.mark.parametrize('condition', ['viewer', 'revoked', 'account_inactive', 'patient_deleted'])
def test_current_permission_is_required_for_creation_and_revision(django_user_model, condition):
    _, patient, _, actor, member = family(django_user_model, 'glucose-permission')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    if condition == 'viewer':
        member.role = 'VIEWER'
        member.save(update_fields=['role'])
    elif condition == 'revoked':
        member.revoked_at = timezone.now()
        member.save(update_fields=['revoked_at'])
    elif condition == 'account_inactive':
        type(actor).objects.filter(pk=actor.pk).update(is_active=False)
    else:
        type(patient).objects.filter(pk=patient.pk).update(deleted_at=timezone.now())
    with pytest.raises(PermissionDenied):
        create_record(patient, actor, payload(), creation_key=uuid4())
    with pytest.raises(PermissionDenied):
        revise_record(patient, actor, record.pk, action='DELETE', expected_revision=0)
    record.refresh_from_db()
    assert record.deleted_at is None and record.revision_number == 0


def test_editing_under_another_patient_cannot_rebind_the_record(django_user_model):
    from apps.patients.models import Patient
    _, patient, _, actor, _ = family(django_user_model, 'glucose-other-patient')
    other = Patient.objects.create(account=actor, display_name='另一份档案')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    with pytest.raises(PermissionDenied):
        revise_record(other, actor, record.pk, action='DELETE', expected_revision=0)
    record.refresh_from_db()
    assert record.patient_id == patient.pk and record.deleted_at is None


@pytest.mark.parametrize('kind', ['LAB_REPORT', 'NURSING', 'invalid'])
def test_manual_creation_cannot_claim_a_report_origin(django_user_model, kind):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-source')
    with pytest.raises(GlucoseInputError):
        create_record(patient, actor, payload(), creation_key=uuid4(), source_kind=kind)
    assert not GlucoseRecord.objects.filter(patient=patient).exists()


def test_correction_cannot_restore_a_deleted_record_and_first_creation_has_no_undo(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-delete')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    with pytest.raises(GlucoseConflict):
        revise_record(patient, actor, record.pk, action='UNDO', expected_revision=0)
    revise_record(patient, actor, record.pk, action='DELETE', expected_revision=0)
    with pytest.raises(GlucoseConflict):
        revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=1, changes=payload(value='7.5'))
