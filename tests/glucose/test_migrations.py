from copy import deepcopy
from datetime import date
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.self_records.payloads import normalize_payload
from tests.self_records.test_payloads import payload


@pytest.mark.django_db(transaction=True)
def test_removal_drops_glucose_history_and_preserves_daily_records_and_source_labs():
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [(app, '0001_initial' if app == 'glucose' else name) for app, name in leaves]
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
        snapshot = {'schema_version': '1.2', 'self_records': [{'id': str(record.pk), 'original_data': original}],
                    'glucose_records': [], 'glucose_record_sources': [], 'glucose_fingerprint': '0' * 64}
        job = before.get_model('exports', 'ExportJob').objects.create(
            patient_id=patient.pk, requested_by_id=account.pk, session_digest='c' * 64,
            snapshot=deepcopy(snapshot), snapshot_digest='d' * 64, expires_at=timestamp)
        share = before.get_model('patients', 'PatientShare').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, creator_revision=0, token_digest='e' * 64,
            scope={'document_ids': [], 'self_record_ids': [str(record.pk)]}, snapshot=deepcopy(snapshot),
            snapshot_digest='d' * 64, expires_at=timestamp)
        export_source = before.get_model('self_records', 'DailyRecordExportSource').objects.create(job_id=job.pk, record_id=record.pk)
        share_source = before.get_model('self_records', 'DailyRecordShareSource').objects.create(share_id=share.pk, record_id=record.pk)
        from tests.labs.helpers import _observation
        from apps.patients.models import Patient
        source_document, observation = _observation(
            Patient.objects.get(pk=patient.pk), date(2026, 8, 1), '6.2',
            code='LAB_GLU', raw_name='葡萄糖', standard_name='葡萄糖', raw_unit='mmol/L')
        glucose = before.get_model('glucose', 'GlucoseRecord').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, updated_by_id=account.pk,
            creation_key=uuid4(), creation_fingerprint='f' * 64, original_data={'raw_value': '6.2'},
            current_data={'raw_value': '6.3'}, source_kind='LAB_REPORT', time_precision='DAY', revision_number=1,
            source_document_id=source_document.pk, source_page_id=observation.document_page_id,
            source_parsing_version_id=observation.parsing_version_id, source_observation_id=observation.pk)
        before.get_model('glucose', 'GlucoseRevision').objects.create(
            record_id=glucose.pk, author_id=account.pk, sequence=1, action='CORRECT',
            before={'raw_value': '6.2'}, after={'raw_value': '6.3'})
        retired_snapshot = {'glucose_records': [{'id': str(glucose.pk), 'data': glucose.current_data}]}
        retired_job = before.get_model('exports', 'ExportJob').objects.create(
            patient_id=patient.pk, requested_by_id=account.pk, session_digest='a' * 64,
            snapshot=retired_snapshot, snapshot_digest='f' * 64, expires_at=timestamp,
            object_key='synthetic-retired-output', filename='retired.zip')
        retired_share = before.get_model('patients', 'PatientShare').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, creator_revision=0, token_digest='f' * 64,
            scope={'glucose_record_ids': [str(glucose.pk)]}, snapshot=retired_snapshot,
            snapshot_digest='f' * 64, expires_at=timestamp)
        before.get_model('glucose', 'GlucoseExportSource').objects.create(job_id=retired_job.pk, record_id=glucose.pk)
        before.get_model('glucose', 'GlucoseShareSource').objects.create(share_id=retired_share.pk, record_id=glucose.pk)
        unbound_job = before.get_model('exports', 'ExportJob').objects.create(
            patient_id=patient.pk, requested_by_id=account.pk, session_digest='a' * 64,
            snapshot=retired_snapshot, snapshot_digest='f' * 64, expires_at=timestamp)
        unbound_share = before.get_model('patients', 'PatientShare').objects.create(
            patient_id=patient.pk, created_by_id=account.pk, creator_revision=0, token_digest='a' * 64,
            scope={'glucose_record_ids': [str(glucose.pk)]}, snapshot={},
            snapshot_digest='f' * 64, expires_at=timestamp)
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
        retired_job.refresh_from_db()
        assert retired_job.snapshot == retired_job.options == {} and retired_job.snapshot_digest == ''
        assert retired_job.status == 'INVALIDATED' and retired_job.cleanup_pending
        assert retired_job.object_key == 'synthetic-retired-output' and retired_job.filename == ''
        retired_share.refresh_from_db()
        assert retired_share.snapshot == retired_share.scope == {} and retired_share.snapshot_digest == ''
        assert retired_share.invalidated_at and retired_share.invalidation_reason == 'feature_removed'
        unbound_job.refresh_from_db()
        assert unbound_job.snapshot == {} and unbound_job.status == 'INVALIDATED'
        unbound_share.refresh_from_db()
        assert unbound_share.scope == {} and unbound_share.invalidated_at
        assert current.get_model('labs', 'LabObservation').objects.get(pk=observation.pk).raw_value == '6.2'
        assert current.get_model('documents', 'Document').objects.filter(pk=source_document.pk).exists()
        for model in ('GlucoseRecord', 'GlucoseRevision', 'GlucoseExportSource', 'GlucoseShareSource'):
            with pytest.raises(LookupError):
                current.get_model('glucose', model)
        assert not any(table.startswith('glucose_') for table in connection.introspection.table_names())
    finally:
        MigrationExecutor(connection).migrate(leaves)
