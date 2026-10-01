"""Removal drops dedicated history while retaining original medical sources."""

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


@pytest.mark.django_db(transaction=True)
def test_removal_drops_treatment_history_and_invalidates_only_affected_outputs(django_user_model):
    executor = MigrationExecutor(connection)
    assert ('treatments', '0006_remove_treatment_feature') in executor.loader.graph.nodes
    previous = [('treatments', '0005_treatmentexportsource_treatmentsharesource')]
    try:
        executor.migrate(previous)
        executor = MigrationExecutor(connection)
        historical = executor.loader.project_state(list(executor.loader.applied_migrations)).apps
        _, patient = _patient(django_user_model, 'treatment-removal-migration')
        document, version = parsed_facts(patient, ['治疗经过：原文手术记录。'])
        event = historical.get_model('treatments', 'TreatmentEvent').objects.create(
            patient_id=patient.pk, origin='USER', source_key='synthetic-event',
            initial_content={'title': '专用历史'}, current_content={'title': '专用历史'})
        regimen = historical.get_model('treatments', 'TreatmentRegimen').objects.create(
            patient_id=patient.pk, origin='USER', source_key='synthetic-regimen',
            initial_content={}, current_content={}, normalized_key='synthetic', episode_key='synthetic')
        regimen.events.add(event)
        historical.get_model('treatments', 'TreatmentCycle').objects.create(
            patient_id=patient.pk, origin='USER', source_key='synthetic-cycle',
            initial_content={}, current_content={}, regimen=regimen)
        jobs = historical.get_model('exports', 'ExportJob')
        shares = historical.get_model('patients', 'PatientShare')
        now = timezone.now()
        retired = {'treatment_events': [{'id': str(event.pk), 'content': event.current_content}]}
        retained = {'documents': [{'id': str(document.pk)}], 'treatment_events': [], 'selection': {}}
        common = {'patient_id': patient.pk, 'snapshot_digest': 'a' * 64, 'expires_at': now + timedelta(hours=24)}
        old_job = jobs.objects.create(**common, snapshot=retired, status='READY', object_key='synthetic-object',
            completed_at=now, sha256='d' * 64, byte_size=1)
        kept_job = jobs.objects.create(**common, snapshot=retained)
        old_share = shares.objects.create(**common, snapshot=retired, scope={'cycle_ids': ['synthetic']},
            token_digest='b' * 64, creator_revision=1)
        kept_share = shares.objects.create(**common, snapshot=retained, token_digest='c' * 64, creator_revision=1)
        bound_job = jobs.objects.create(**common, snapshot={})
        option_job = jobs.objects.create(**common, snapshot={}, options={'cycle_ids': ['synthetic-cycle']})
        bound_share = shares.objects.create(**common, snapshot={}, token_digest='e' * 64, creator_revision=1)
        historical.get_model('treatments', 'TreatmentExportSource').objects.create(job=bound_job, event=event)
        historical.get_model('treatments', 'TreatmentShareSource').objects.create(share=bound_share, event=event)
        before = {table for table in connection.introspection.table_names() if table.startswith('treatments_')}
        assert len(before) >= 11
        executor.migrate([('treatments', '0006_remove_treatment_feature')])
        assert not before.intersection(connection.introspection.table_names())
        assert historical.get_model('documents', 'Document').objects.filter(pk=document.pk).exists()
        assert historical.get_model('facts', 'Fact').objects.filter(parsing_version_id=version.pk, category='TREATMENT').exists()
        old_job.refresh_from_db()
        old_share.refresh_from_db()
        kept_job.refresh_from_db()
        kept_share.refresh_from_db()
        assert old_job.status == 'INVALIDATED' and old_job.snapshot == {} and old_job.cleanup_pending
        assert old_job.object_key == 'synthetic-object'
        assert old_share.invalidated_at is not None and old_share.snapshot == {} and old_share.scope == {}
        assert kept_job.snapshot == retained and kept_share.snapshot == retained
        assert kept_share.invalidated_at is None
        bound_job.refresh_from_db()
        option_job.refresh_from_db()
        bound_share.refresh_from_db()
        assert bound_job.status == 'INVALIDATED' and bound_job.cleanup_pending
        assert option_job.status == 'INVALIDATED' and option_job.options == {}
        assert bound_share.invalidated_at is not None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
