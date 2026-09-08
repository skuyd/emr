from uuid import uuid4

import pytest

from apps.glucose.models import GlucoseRecord, GlucoseRevision
from apps.glucose.services import create_record, revise_record
from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from tests.glucose.test_payloads import payload
from tests.patients.test_family_access import family


pytestmark = pytest.mark.django_db


def test_patient_purge_cleans_glucose_history_and_preserves_pseudonymous_audit(django_user_model):
    from apps.patients.deletion import request_patient_deletion, purge_patient_deletions
    from apps.patients.models import Patient
    _, patient, _, actor, _ = family(django_user_model, 'glucose-patient-purge')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='7.2'))
    identity = patient.pk
    request_patient_deletion(identity, patient.account, document_dispatch=lambda _: None)
    assert purge_patient_deletions(patient_ids=[identity]) == 1
    assert not Patient.objects.filter(pk=identity).exists()
    assert not GlucoseRecord.objects.filter(pk=record.pk).exists()
    assert not GlucoseRevision.objects.filter(record_id=record.pk).exists()
    event = AuditEvent.objects.get(patient_hash=_hash('patient', identity), action='glucose_record_created')
    assert event.resource_type == 'glucose_record'


def test_purged_contributor_leaves_other_family_records_and_anonymized_authors(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion, AccountDeletionOutcome
    from apps.accounts.models import AccountDeletionJob
    _, patient, _, actor, _ = family(django_user_model, 'glucose-author-purge')
    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='7.2'))
    author_id = actor.pk
    request_account_deletion(author_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    job = AccountDeletionJob.objects.get(account_id=author_id)
    assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
    record.refresh_from_db()
    assert record.created_by_id is None and record.updated_by_id is None
    assert record.revisions.get().author_id is None
    assert record.original_data['raw_value'] == '5.50' and record.current_data['raw_value'] == '7.2'
    assert AuditEvent.objects.filter(actor_hash=_hash('actor', author_id), action='glucose_record_revised').exists()
