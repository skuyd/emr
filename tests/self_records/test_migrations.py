from copy import deepcopy
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.self_records.payloads import normalize_payload
from tests.self_records.test_payloads import payload


@pytest.mark.django_db(transaction=True)
def test_source_binding_migration_preserves_existing_records_revisions_authors_and_snapshots():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [(app, '0001_initial' if app == 'self_records' else name) for app, name in leaves]
    try:
        executor.migrate(previous)
        before = executor.loader.project_state(previous).apps
        account = before.get_model('accounts', 'Account').objects.create(phone_hash='a' * 64, phone_encrypted='synthetic')
        patient = before.get_model('patients', 'Patient').objects.create(account_id=account.pk, display_name='迁移合成患者')
        original = normalize_payload(payload(value='98.6', kind='TEMPERATURE', unit='°F'))
        corrected = normalize_payload(payload(value='37.5', kind='TEMPERATURE', unit='°C'))
        timestamp = timezone.now()
        record = before.get_model('self_records', 'DailyRecord').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, updated_by_id=account.pk,
            creation_key=uuid4(), creation_fingerprint='b' * 64, original_data=original, current_data=corrected,
            kind='TEMPERATURE', measured_at=corrected['measured_at'], revision_number=1,
            created_at=timestamp, updated_at=timestamp,
        )
        revision = before.get_model('self_records', 'DailyRecordRevision').objects.create(
            record_id=record.pk, author_id=account.pk, sequence=1, action='CORRECT',
            before={'data': original, 'deleted_at': None}, after={'data': corrected, 'deleted_at': None}, created_at=timestamp,
        )
        snapshot = {'schema_version': '1.1', 'synthetic_migration_marker': str(record.pk)}
        job = before.get_model('exports', 'ExportJob').objects.create(
            patient_id=patient.pk, requested_by_id=account.pk, session_digest='c' * 64,
            snapshot=deepcopy(snapshot), snapshot_digest='d' * 64, expires_at=timestamp,
        )
        share = before.get_model('patients', 'PatientShare').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, creator_revision=0, token_digest='e' * 64,
            scope={'document_ids': []}, snapshot=deepcopy(snapshot), snapshot_digest='d' * 64, expires_at=timestamp,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        restored = current.get_model('self_records', 'DailyRecord').objects.get(pk=record.pk)
        assert restored.patient_id == patient.pk and restored.created_by_id == restored.updated_by_id == account.pk
        assert restored.original_data == original and restored.current_data == corrected
        assert restored.creation_key == record.creation_key and restored.creation_fingerprint == 'b' * 64
        saved_revision = current.get_model('self_records', 'DailyRecordRevision').objects.get(pk=revision.pk)
        assert saved_revision.author_id == account.pk and saved_revision.sequence == restored.revision_number == 1
        assert saved_revision.before == revision.before and saved_revision.after == revision.after
        assert current.get_model('exports', 'ExportJob').objects.get(pk=job.pk).snapshot == snapshot
        assert current.get_model('patients', 'PatientShare').objects.get(pk=share.pk).snapshot == snapshot
        for model, key, identity in [('DailyRecordExportSource', 'job_id', job.pk), ('DailyRecordShareSource', 'share_id', share.pk)]:
            binding = current.get_model('self_records', model)
            assert not binding.objects.exists()
            assert binding.objects.create(record_id=record.pk, **{key: identity}).record_id == record.pk
    finally:
        MigrationExecutor(connection).migrate(leaves)
