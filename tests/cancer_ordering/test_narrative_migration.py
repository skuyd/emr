"""The new tables and XOR leave real preexisting archives and decisions intact."""
from copy import deepcopy

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import candidate_rows
from apps.cancer_ordering.services import collect_current
from tests.cancer_ordering.test_migrations import PRESERVED_MODELS
from tests.cancer_ordering.test_services import _revise, _select
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


@pytest.mark.django_db(transaction=True)
def test_narrative_migration_preserves_existing_fact_sources_review_and_selection(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-migration-archive')
    parsed_facts(patient, ['出院诊断：肺癌。'])
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(patient=patient)
    _revise(patient, candidate_rows(patient)[0], 'CONFIRM', checked_original=True)
    _select(patient, 'CANDIDATE', candidate_id=candidate.pk)
    model_names = (*PRESERVED_MODELS, *(('cancer_ordering', name) for name in (
        'CancerCandidate', 'CandidateRevision', 'CollectionRun', 'CollectionCandidate', 'DisplaySelection', 'SelectionRevision')))
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    previous = [(app, '0001_initial' if app == 'cancer_ordering' else name) for app, name in leaves]
    try:
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        before = {f'{app}.{model}': list(old.get_model(app, model).objects.order_by('pk').values()) for app, model in model_names}
        for name in ('CancerCandidate', 'CandidateRevision', 'CollectionRun', 'CollectionCandidate', 'DisplaySelection', 'SelectionRevision'):
            assert before['cancer_ordering.' + name]
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        current = executor.loader.project_state(leaves).apps
        for app, model in model_names:
            old_model = old.get_model(app, model)
            columns = [field.attname for field in old_model._meta.fields]
            assert list(current.get_model(app, model).objects.order_by('pk').values(*columns)) == before[f'{app}.{model}']
        assert not current.get_model('cancer_ordering', 'NarrativeSource').objects.exists()
        assert not current.get_model('cancer_ordering', 'NarrativeDependency').objects.exists()
        retained = current.get_model('cancer_ordering', 'CancerCandidate').objects.get(pk=candidate.pk)
        assert retained.source_fact_id == candidate.source_fact_id and retained.source_narrative_id is None
    finally:
        MigrationExecutor(connection).migrate(leaves)
