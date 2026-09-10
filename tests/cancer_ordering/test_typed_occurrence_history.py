"""Deleting/replacing a source cannot erase a reviewed original occurrence."""
from copy import deepcopy
import uuid

import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.facts.models import Fact, FactSourceFragment
from tests.cancer_ordering.test_services import _revise
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture


pytestmark = pytest.mark.django_db


def replacement(field):
    material = {f.attname: deepcopy(getattr(field, f.attname)) for f in field._meta.fields
                if f.name not in {'id', 'created_at'}}
    pieces = [{f.attname: deepcopy(getattr(piece, f.attname)) for f in piece._meta.fields
               if f.name not in {'id', 'fact'}} for piece in field.source_fragments.all()]
    material['revision_number'] = 0
    new = Fact.objects.create(id=uuid.uuid4(), **material)
    for piece in pieces:
        FactSourceFragment.objects.create(fact=new, **piece)
    new.full_clean()
    field.delete()
    return new


@pytest.mark.parametrize('action', ['EXCLUDE', 'DEFER', 'CORRECT_REVOKE'])
def test_same_original_field_replacement_keeps_prior_review_barrier(django_user_model, action):
    _, patient, _, _, field, _ = typed_fixture(django_user_model)
    collect_current(patient, actor=patient.account)
    row = next(row for row in candidate_rows(patient) if CancerCandidate.objects.get(pk=row['id']).source_fact_id == field.pk)
    original_id = row['id']
    if action == 'CORRECT_REVOKE':
        _revise(patient, row, 'CORRECT', checked_original=True, reason='合成原件核对',
                changes={'label': '肺癌', 'profile': 'LUNG', 'assertion': 'AFFIRMED', 'subject': 'CURRENT_PRIMARY'})
        _revise(patient, next(row for row in candidate_rows(patient) if row['id'] == original_id), 'REVOKE')
    else:
        _revise(patient, row, action)
    new = replacement(field)
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.filter(source_fact=new).exists()
    assert not CancerCandidate.objects.filter(pk=original_id).exists()
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    assert not any(row['eligible_for_auto'] for row in candidate_rows(patient))
