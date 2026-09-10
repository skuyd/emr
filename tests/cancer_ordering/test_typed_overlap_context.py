"""Same-position legacy excerpts cannot bypass a typed context review."""
import pytest

from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture

pytestmark = pytest.mark.django_db


def test_unreviewed_typed_anchor_blocks_same_position_legacy_excerpt(django_user_model):
    _, patient, _, _, field, _ = typed_fixture(django_user_model, text='肺癌。',
        heading='病理诊断', confirm_anchor=False)
    runs = collect_current(patient, actor=patient.account)
    members = [member for run in runs for member in run.members.all()]
    assert len(members) == 1
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    assert members[0].candidate.source_fact.representation == 'EXCERPT'


def test_distinct_unbounded_excerpt_is_not_deduplicated_by_label(django_user_model):
    # No punctuation closes this excerpt before the next raw QC line; it is a
    # different unsupported bounded clause, not the typed value's OCR range.
    _, patient, _, _, _, _ = typed_fixture(django_user_model, heading='病理诊断')
    runs = collect_current(patient, actor=patient.account)
    assert sum(run.members.count() for run in runs) == 2
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_malformed_typed_fragment_reference_does_not_disappear_from_legacy_dependencies(django_user_model):
    from copy import deepcopy
    from apps.facts.models import Fact
    _, patient, _, _, field, _ = typed_fixture(django_user_model, text='肺癌。', heading='病理诊断')
    content = deepcopy(field.automatic_content)
    content['literal_source']['value_fragment_ordinals'] = [999]
    Fact.objects.filter(pk=field.pk).update(automatic_content=content)
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
