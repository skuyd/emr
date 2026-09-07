from uuid import uuid4

import pytest

from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.self_records.models import DailyRecord, DailyRecordRevision
from apps.self_records.services import create_record, revise_record
from tests.documents.fakes import InMemoryObjectStore
from tests.patients.test_family_access import family
from tests.self_records.test_export_integration import selection
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


def test_patient_purge_removes_records_history_and_source_bindings_after_export_cleanup(django_user_model):
    from apps.exports.services import create_preview, cleanup_export
    from apps.patients.deletion import request_patient_deletion, purge_patient_deletions
    from apps.patients.models import Patient
    _, patient, client, actor, _ = family(django_user_model, 'daily-patient-purge')
    record = create_record(patient, actor, payload(notes='erase this record'), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='61'))
    job = create_preview(patient, client.session.session_key, selection(record), actor=actor)
    identity = patient.pk
    request_patient_deletion(patient.pk, patient.account, document_dispatch=lambda _: None)
    job.refresh_from_db()
    assert job.snapshot == {} and job.cleanup_pending
    assert client.get(f'/self-records/{record.pk}/').status_code == 404
    assert purge_patient_deletions(patient_ids=[identity]) == 0
    cleanup_export(job.pk, InMemoryObjectStore())
    assert purge_patient_deletions(patient_ids=[identity]) == 1
    assert not Patient.objects.filter(pk=identity).exists()
    assert not DailyRecord.objects.filter(pk=record.pk).exists() and not DailyRecordRevision.objects.filter(record_id=record.pk).exists()
    assert AuditEvent.objects.filter(patient_hash=_hash('patient', identity), action='self_record_created').exists()


def test_purging_a_contributor_preserves_other_familys_records_and_anonymizes_revision_authors(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion, AccountDeletionOutcome
    from apps.accounts.models import AccountDeletionJob
    owner, patient, _, actor, _ = family(django_user_model, 'daily-contributor-purge')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='61'))
    actor_id = actor.pk
    request_account_deletion(actor_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    job = AccountDeletionJob.objects.get(account_id=actor_id)
    assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
    record.refresh_from_db()
    assert record.created_by_id is None and record.updated_by_id is None
    assert record.revisions.get().author_id is None and record.current_data['raw_value'] == '61'
    assert record.original_data['raw_value'] == '60.0'
    assert owner.get(f'/self-records/{record.pk}/').status_code == 200
    assert AuditEvent.objects.filter(actor_hash=_hash('actor', actor_id), action='self_record_revised').exists()


def test_unselected_record_revision_does_not_invalidate_selected_export(django_user_model):
    from apps.exports.services import create_preview, get_preview
    _, patient, client, actor, _ = family(django_user_model, 'daily-unselected-live')
    chosen = create_record(patient, actor, payload(), creation_key=uuid4()).record
    other = create_record(patient, actor, payload(notes='not selected'), creation_key=uuid4()).record
    job = create_preview(patient, client.session.session_key, selection(chosen), actor=actor)
    revise_record(patient, actor, other.pk, action='DELETE', expected_revision=0)
    current = get_preview(patient, client.session.session_key, job.pk, actor=actor)
    assert current.status == 'PREVIEW' and [row['id'] for row in current.snapshot['self_records']] == [str(chosen.pk)]
