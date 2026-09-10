from copy import deepcopy
from datetime import date, timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
import pytest

from apps.exports.models import ExportJob, ExportSource
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientPreference, PatientShare, ShareSource
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.labs.test_trends import _observation


PRESERVED_MODELS = (
    ('accounts', 'Account'), ('patients', 'Patient'), ('patients', 'PatientMembership'),
    ('patients', 'PatientPreference'), ('documents', 'Document'), ('documents', 'DocumentPage'),
    ('processing', 'ParsingVersion'), ('processing', 'OcrBlock'), ('processing', 'SourceEvidence'),
    ('facts', 'Fact'), ('facts', 'FactRevision'), ('facts', 'FactExtraction'),
    ('labs', 'LabObservation'), ('exports', 'ExportJob'), ('exports', 'ExportSource'),
    ('patients', 'PatientShare'), ('patients', 'ShareSource'),
)


def _rows(registry):
    return {f'{app}.{model}': list(registry.get_model(app, model).objects.order_by('pk').values())
            for app, model in PRESERVED_MODELS}


@pytest.mark.django_db(transaction=True)
def test_initial_migration_preserves_sources_history_preferences_and_saved_outputs(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-migration')
    document, version = parsed_facts(patient, ['出院诊断：肺癌。'])
    fact = Fact.objects.get(parsing_version=version, representation='EXCERPT')
    revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM',
                expected_revision=0, checked_original=True)
    lab_document, observation = _observation(patient, date(2026, 8, 20), '4.20')
    now = timezone.now()
    PatientPreference.objects.update_or_create(patient=patient, account=patient.account, defaults={
        'browser_notifications_enabled': True, 'browser_notification_prompted_at': now})
    snapshot = {'schema_version': '1.4', 'facts': [{'id': str(fact.pk), 'raw_text': fact.raw_text}],
                'observations': [{'id': str(observation.pk), 'raw_value': '4.20'}]}
    job = ExportJob.objects.create(patient=patient, requested_by=patient.account, session_digest='1' * 64,
        snapshot=deepcopy(snapshot), snapshot_digest='2' * 64, expires_at=now + timedelta(hours=1))
    share = PatientShare.objects.create(patient=patient, created_by=patient.account, creator_revision=0,
        token_digest='3' * 64, scope={'document_ids': [str(document.pk), str(lab_document.pk)]},
        snapshot=deepcopy(snapshot), snapshot_digest='2' * 64, expires_at=now + timedelta(hours=1))
    for source_document in (document, lab_document):
        ExportSource.objects.create(job=job, document=source_document)
        ShareSource.objects.create(share=share, document=source_document)

    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [(app, None if app == 'cancer_ordering' else name) for app, name in leaves]
    try:
        executor.migrate(previous)
        old = executor.loader.project_state([target for target in previous if target[1] is not None]).apps
        before = _rows(old)
        assert all(before.values())  # Every preservation assertion has a real legacy row.
        assert not any(name.startswith('cancer_ordering_') for name in connection.introspection.table_names())
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        assert _rows(current) == before
        for model in ('CancerCandidate', 'CandidateRevision', 'CollectionRun', 'CollectionCandidate',
                      'DisplaySelection', 'SelectionRevision'):
            assert not current.get_model('cancer_ordering', model).objects.exists()
    finally:
        MigrationExecutor(connection).migrate(leaves)
