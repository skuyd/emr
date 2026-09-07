from uuid import uuid4

import pytest

from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.self_records.services import create_record, revise_record
from tests.patients.test_family_access import family
from tests.self_records.test_payloads import payload


@pytest.mark.django_db
def test_creation_and_revisions_audit_actual_actor_patient_and_no_record_body(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'self-audit')
    record = create_record(patient, actor, payload(notes='private measurement note'), creation_key=uuid4()).record
    revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0, changes=payload(value='62.345', notes='private correction'))
    revise_record(patient, actor, record.pk, action='DELETE', expected_revision=1)
    revise_record(patient, actor, record.pk, action='UNDO', expected_revision=2)
    events = list(AuditEvent.objects.filter(target_hash=_hash('target', record.pk)).order_by('created_at'))
    assert [row.action for row in events] == ['self_record_created', 'self_record_revised', 'self_record_revised', 'self_record_revised']
    assert [row.reason_code for row in events] == ['', 'correct', 'delete', 'undo']
    assert all(row.actor_hash == _hash('actor', actor.pk) and row.patient_hash == _hash('patient', patient.pk)
               and row.resource_type == 'self_record' and row.result == 'succeeded' for row in events)
    persisted = str([row.__dict__ for row in events])
    assert 'private measurement note' not in persisted and 'private correction' not in persisted and '62.345' not in persisted
