from copy import deepcopy
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.self_records.models import DailyRecord
from apps.self_records.services import RecordConflict, create_record, revise_record
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


def test_editor_records_actual_author_and_retries_preserve_original_creation(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'self-create')
    key = uuid4()
    created = create_record(patient, actor, payload(), creation_key=key)
    assert created.created and created.record.created_by_id == actor.pk
    assert created.record.patient_id == patient.pk
    assert created.record.original_data['raw_value'] == '60.0'
    original = deepcopy(created.record.original_data)
    revise_record(patient, actor, created.record.pk, action='CORRECT', expected_revision=0, changes=payload(value='61'))
    replay = create_record(patient, actor, payload(), creation_key=key)
    assert not replay.created and replay.record.pk == created.record.pk
    assert replay.record.original_data == original
    assert replay.record.current_data['raw_value'] == '61'
    assert DailyRecord.objects.filter(patient=patient).count() == 1


def test_same_minute_measurements_keep_distinct_identities(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'self-same-minute')
    first = create_record(patient, actor, payload(), creation_key=uuid4()).record
    second = create_record(patient, actor, payload(), creation_key=uuid4()).record
    assert first.pk != second.pk and first.measured_at == second.measured_at


def test_reusing_creation_key_with_changed_input_conflicts(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'self-key-conflict')
    key = uuid4()
    create_record(patient, actor, payload(), creation_key=key)
    with pytest.raises(RecordConflict):
        create_record(patient, actor, payload(value='62'), creation_key=key)
    assert DailyRecord.objects.filter(patient=patient).count() == 1


def test_correction_delete_and_undo_preserve_original_and_each_author(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'self-history')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    original = deepcopy(record.original_data)
    corrected = revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='63', notes='复称'))
    assert corrected.revision_number == 1 and corrected.current_data['raw_value'] == '63'
    deleted = revise_record(patient, patient.account, record.pk, action='DELETE', expected_revision=1)
    assert deleted.deleted_at is not None
    restored = revise_record(patient, actor, record.pk, action='UNDO', expected_revision=2)
    assert restored.deleted_at is None and restored.current_data['raw_value'] == '63'
    assert restored.original_data == original
    revisions = list(restored.revisions.order_by('sequence'))
    assert [row.action for row in revisions] == ['CORRECT', 'DELETE', 'UNDO']
    assert [row.author_id for row in revisions] == [actor.pk, patient.account_id, actor.pk]
    assert revisions[0].before['data']['raw_value'] == '60.0'


@pytest.mark.parametrize('revision', [True, -1, '0', 1])
def test_invalid_or_stale_revision_does_not_append_history(django_user_model, revision):
    _, patient, _, actor, _ = family(django_user_model, 'self-revision')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    with pytest.raises(RecordConflict):
        revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=revision, changes=payload(value='9'))
    record.refresh_from_db()
    assert record.revision_number == 0 and not record.revisions.exists()
    assert record.current_data['raw_value'] == '60.0'


@pytest.mark.parametrize('condition', ['viewer', 'revoked', 'account_inactive', 'patient_deleted'])
def test_write_authorization_is_reloaded_from_current_database(django_user_model, condition):
    _, patient, _, actor, member = family(django_user_model, 'self-permission')
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


def test_patient_context_cannot_be_substituted_for_another_record(django_user_model):
    _, first, _, actor, _ = family(django_user_model, 'self-first')
    from apps.patients.models import Patient
    second = Patient.objects.create(account=actor, display_name='另一份档案')
    record = create_record(first, actor, payload(), creation_key=uuid4()).record
    with pytest.raises(PermissionDenied):
        revise_record(second, actor, record.pk, action='DELETE', expected_revision=0)
    record.refresh_from_db()
    assert record.patient_id == first.pk and record.deleted_at is None


def test_deletion_cannot_be_silently_reversed_by_correction(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'self-deleted')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='DELETE', expected_revision=0)
    with pytest.raises(RecordConflict):
        revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=1, changes=payload(value='64'))
    record.refresh_from_db()
    assert record.deleted_at is not None and record.revision_number == 1
