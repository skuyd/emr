"""Unknown original position is not proof that old review has no overlap."""
from copy import deepcopy
import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.facts.models import Fact
from tests.cancer_ordering.test_services import _revise
from tests.cancer_ordering.test_typed_occurrence_history import replacement
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('action', ['EXCLUDE', 'DEFER'])
def test_restored_source_proof_does_not_clear_an_unlocated_same_page_review(django_user_model, action):
    _, patient, _, _, field, _ = typed_fixture(django_user_model)
    legacy = deepcopy(field.automatic_content)
    del legacy['literal_source']
    Fact.objects.filter(pk=field.pk).update(automatic_content=legacy)
    collect_current(patient, actor=patient.account)
    original = CancerCandidate.objects.get(source_fact=field)
    row = next(row for row in candidate_rows(patient) if row['id'] == str(original.pk))
    assert row['binding_kind'] == 'PAGE_ONLY'
    _revise(patient, row, action)
    # The cached field retains its genuine extracted roles; the replacement has
    # current proof, while the removed source could not establish disjointness.
    replacement(field)
    collect_current(patient, actor=patient.account)
    assert not CancerCandidate.objects.filter(pk=original.pk).exists()
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
