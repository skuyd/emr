"""Only already stored review/position snapshots are copied during migration."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import candidate_rows
from apps.cancer_ordering.services import collect_current
from tests.cancer_ordering.test_migrations import PRESERVED_MODELS
from tests.cancer_ordering.test_services import _revise
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


@pytest.mark.django_db(transaction=True)
def test_migration_preserves_all_old_rows_and_retains_review_after_parent_deletion(django_user_model):
    _, patient = _patient(django_user_model, 'occurrence-migration')
    parsed_facts(patient, ['出院诊断：肺癌。'])
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(patient=patient)
    _revise(patient, candidate_rows(patient)[0], 'EXCLUDE')
    names = (*PRESERVED_MODELS, *(('cancer_ordering', name) for name in (
        'CancerCandidate', 'CandidateRevision', 'CollectionRun', 'CollectionCandidate', 'DisplaySelection', 'SelectionRevision')))
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    old_targets = [(app, '0002_narrative_sources' if app == 'cancer_ordering' else name) for app, name in leaves]
    try:
        executor.migrate(old_targets)
        old = executor.loader.project_state(old_targets).apps
        before = {f'{app}.{name}': list(old.get_model(app, name).objects.order_by('pk').values()) for app, name in names}
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)
        new = executor.loader.project_state(leaves).apps
        for app, name in names:
            assert list(new.get_model(app, name).objects.order_by('pk').values()) == before[f'{app}.{name}']
        review = new.get_model('cancer_ordering', 'OccurrenceReview').objects.get()
        original = old.get_model('cancer_ordering', 'CandidateRevision').objects.get()
        assert review.original_revision_id == original.pk
        assert review.original_candidate_id == candidate.pk
        assert review.action == 'EXCLUDE' and review.after == original.after and review.before == original.before
        new.get_model('facts', 'Fact').objects.get(pk=candidate.source_fact_id).delete()
        review.refresh_from_db()
        assert review.candidate_id is None and review.original_candidate_id == candidate.pk
        assert review.after['status'] == 'EXCLUDED'
    finally:
        MigrationExecutor(connection).migrate(leaves)
