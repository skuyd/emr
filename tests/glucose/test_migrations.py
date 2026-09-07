from copy import deepcopy
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.self_records.payloads import normalize_payload
from tests.self_records.test_payloads import payload


@pytest.mark.django_db(transaction=True)
def test_glucose_migration_preserves_daily_history_existing_selections_and_opaque_identities():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [(app, None if app == 'glucose' else name) for app, name in leaves]
    try:
        executor.migrate(previous)
        before = executor.loader.project_state([target for target in previous if target[1] is not None]).apps
        account = before.get_model('accounts', 'Account').objects.create(phone_hash='7' * 64, phone_encrypted='synthetic')
        patient = before.get_model('patients', 'Patient').objects.create(account_id=account.pk, display_name='迁移合成患者')
        timestamp = timezone.now()
        original = normalize_payload(payload(value='60'))
        corrected = normalize_payload(payload(value='61'))
        record = before.get_model('self_records', 'DailyRecord').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, updated_by_id=account.pk,
            creation_key=uuid4(), creation_fingerprint='b' * 64, original_data=original, current_data=corrected,
            kind='WEIGHT', measured_at=corrected['measured_at'], revision_number=1,
            created_at=timestamp, updated_at=timestamp)
        revision = before.get_model('self_records', 'DailyRecordRevision').objects.create(
            record_id=record.pk, author_id=account.pk, sequence=1, action='CORRECT',
            before={'data': original, 'deleted_at': None}, after={'data': corrected, 'deleted_at': None})
        snapshot = {'schema_version': '1.2', 'self_records': [{'id': str(record.pk), 'original_data': original}]}
        job = before.get_model('exports', 'ExportJob').objects.create(
            patient_id=patient.pk, requested_by_id=account.pk, session_digest='c' * 64,
            snapshot=deepcopy(snapshot), snapshot_digest='d' * 64, expires_at=timestamp)
        share = before.get_model('patients', 'PatientShare').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, creator_revision=0, token_digest='e' * 64,
            scope={'document_ids': [], 'self_record_ids': [str(record.pk)]}, snapshot=deepcopy(snapshot),
            snapshot_digest='d' * 64, expires_at=timestamp)
        export_source = before.get_model('self_records', 'DailyRecordExportSource').objects.create(job_id=job.pk, record_id=record.pk)
        share_source = before.get_model('self_records', 'DailyRecordShareSource').objects.create(share_id=share.pk, record_id=record.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        preserved = current.get_model('self_records', 'DailyRecord').objects.get(pk=record.pk)
        assert preserved.original_data == original and preserved.current_data == corrected
        assert preserved.created_by_id == preserved.updated_by_id == account.pk
        assert preserved.creation_key == record.creation_key and preserved.creation_fingerprint == 'b' * 64
        old_revision = current.get_model('self_records', 'DailyRecordRevision').objects.get(pk=revision.pk)
        assert old_revision.before == revision.before and old_revision.after == revision.after
        assert old_revision.author_id == account.pk and old_revision.sequence == preserved.revision_number == 1
        assert current.get_model('exports', 'ExportJob').objects.get(pk=job.pk).snapshot == snapshot
        assert current.get_model('patients', 'PatientShare').objects.get(pk=share.pk).snapshot == snapshot
        assert current.get_model('self_records', 'DailyRecordExportSource').objects.get(pk=export_source.pk).record_id == record.pk
        assert current.get_model('self_records', 'DailyRecordShareSource').objects.get(pk=share_source.pk).record_id == record.pk
        for model in ('GlucoseRecord', 'GlucoseRevision', 'GlucoseExportSource', 'GlucoseShareSource'):
            assert not current.get_model('glucose', model).objects.exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)
