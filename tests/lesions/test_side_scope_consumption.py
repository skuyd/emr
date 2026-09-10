"""Only an explicitly verified whole-group side may qualify a match reason."""
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.lesions.proposals import propose_matches
from apps.lesions.readmodels import review_observations, review_proposals
from apps.lesions.models import LesionMatchProposal, Lesion
from apps.lesions.services import decide_proposal, generate_proposals
from tests.lesions.test_proposals import observed
from tests.lesions.test_proposal_decisions import arguments, pair
from tests.lesions.test_relationships import expectations


def scoped(identity, *, side='LEFT', state='WHOLE_ENTITY'):
    row = observed(identity, site='合成完整位置组', side=side)
    parent, field = row['fields']
    for value in row['fields']:
        value.update(status='CONFIRMED', source_valid=True)
    field['laterality_scope'] = {'scope_state': state, 'binding_id': identity + ':scope',
        'parent_id': parent['id'], 'valid': True, 'parent_usable': True, 'members': []}
    return row


@pytest.mark.parametrize('other_side', ['LEFT', 'RIGHT'])
def test_old_unknown_scope_neither_agrees_with_nor_rejects_another_side(other_side):
    first, second = scoped('one', state='UNKNOWN_SCOPE'), scoped('two', side=other_side, state='UNKNOWN_SCOPE')
    initial = deepcopy([first, second])
    result = propose_matches([first, second])
    assert len(result) == 1
    assert 'explicit_side_equal' not in {reason['code'] for reason in result[0].reasons}
    assert 'side_scope_not_whole' in result[0].blockers
    assert [first, second] == initial


def test_named_members_stay_separate_and_never_qualify_as_whole_bilateral():
    from apps.facts.laterality_consumption import effective_laterality

    row = scoped('one', state='NAMED_MEMBERS_ONLY')
    field = row['fields'][1]
    members = [{'member_key': 'member:001', 'site_text': '左肺', 'raw': '左肺', 'code': 'LEFT'},
               {'member_key': 'member:002', 'site_text': '右肾', 'raw': '右肾', 'code': 'RIGHT'}]
    field.update(field_key='lesion.scoped_laterality')
    field['content']['value'] = {'scope': 'NAMED_MEMBERS_ONLY', 'members': members}
    field['laterality_scope']['members'] = deepcopy(members)
    value = effective_laterality(row['fields'], [row['fields'][0]])
    assert value['scalar'] is None and value['scope_state'] == 'NAMED_MEMBERS_ONLY'
    assert value['named_members'][0]['members'] == members
    assert value['named_members'][0]['field_id'] == field['id']
    second = deepcopy(row)
    second.update(id='two', report_id='other-report')
    result = propose_matches([row, second])
    assert len(result) == 1
    assert 'side_scope_not_whole' in result[0].blockers
    assert 'explicit_side_equal' not in {reason['code'] for reason in result[0].reasons}


@pytest.mark.parametrize('change', ['missing_parent', 'unconfirmed_parent', 'conflicting_parent', 'source_invalid', 'not_usable', 'unknown_scope'])
def test_unverified_scope_cannot_supply_the_observation_scalar(change):
    from apps.facts.laterality_consumption import effective_laterality

    row = scoped('one')
    parent, field = row['fields']
    parents = [parent]
    if change == 'missing_parent':
        parents = []
    elif change == 'unconfirmed_parent':
        parent['usable'] = False
    elif change == 'conflicting_parent':
        parent['conflict'] = True
    elif change == 'source_invalid':
        field['source_valid'] = False
    elif change == 'not_usable':
        field['usable'] = False
    else:
        field.pop('laterality_scope')
    assert effective_laterality(row['fields'], parents)['scalar'] is None


def test_current_whole_source_can_agree_or_conflict_without_changing_original_fields():
    from apps.facts.laterality_consumption import effective_laterality

    first, second = scoped('one'), scoped('two')
    original = deepcopy(first)
    value = effective_laterality(first['fields'], [first['fields'][0]])
    assert value['scalar'] == {'code': 'LEFT', 'raw': '合成完整位置组'}
    result = propose_matches([first, second])
    assert 'explicit_side_equal' in {reason['code'] for reason in result[0].reasons}
    second['fields'][1]['content']['value']['code'] = 'RIGHT'
    assert propose_matches([first, second]) == []
    assert first == original


@pytest.mark.django_db
def test_old_proposal_rule_requires_regeneration_for_new_acceptance(django_user_model):
    patient = pair(django_user_model, 'scope-rule-outdated')
    generate_proposals(patient, actor=patient.account)
    proposal = LesionMatchProposal.objects.get()
    # Model an existing pre-scope proposal without changing its recorded source
    # fields, decisions or original explanation.
    LesionMatchProposal.objects.filter(pk=proposal.pk).update(rule_version='explicit_location_candidates_v1')
    proposal.refresh_from_db()
    original_reasons = deepcopy(proposal.reasons)
    state = review_proposals(patient, actor=patient.account)[0]
    assert state['source_current'] and not state['rule_current']
    assert state['status'] == 'RULE_OUTDATED' and state['reasons'] == []
    assert state['original_reasons'] == original_reasons
    with pytest.raises(ValidationError, match='规则'):
        decide_proposal(patient, actor=patient.account, action='CONFIRM', **arguments(proposal),
            expectations=expectations(review_observations(patient, actor=patient.account)),
            checked_original=True, name='合成观察')
    assert not Lesion.objects.exists()
    assert proposal.revisions.count() == 1


@pytest.mark.django_db
def test_new_rule_does_not_rebind_a_previously_confirmed_manual_relationship(django_user_model):
    patient = pair(django_user_model, 'scope-rule-manual-link')
    generate_proposals(patient, actor=patient.account)
    proposal = LesionMatchProposal.objects.get()
    decide_proposal(patient, actor=patient.account, action='CONFIRM', **arguments(proposal),
        expectations=expectations(review_observations(patient, actor=patient.account)), checked_original=True, name='合成观察')
    previous = deepcopy(review_observations(patient, actor=patient.account))
    LesionMatchProposal.objects.filter(pk=proposal.pk).update(rule_version='explicit_location_candidates_v1')
    state = review_proposals(patient, actor=patient.account)[0]
    assert state['status'] == state['decision'] == 'CONFIRMED' and not state['rule_current']
    assert state['reasons'] == [] and state['original_reasons']
    assert review_observations(patient, actor=patient.account) == previous


@pytest.mark.django_db
def test_legacy_scalar_remains_visible_but_observation_does_not_flatten_its_scope(django_user_model):
    from apps.facts.models import LateralityScopeBinding
    from tests.lesions.factories import imaging_observation
    from tests.facts.test_scoped_laterality import confirm

    patient, _, report = imaging_observation(django_user_model, name='old-side-read', confirmed=False)
    side = report.fields.get(field_key='lesion.laterality')
    LateralityScopeBinding.objects.filter(fact=side).delete()
    for field in report.fields.all():
        confirm(patient, field)
    original = deepcopy(side.automatic_content)
    row = review_observations(patient, actor=patient.account)[0]
    assert row['laterality'] is None
    assert row['laterality_scope']['scope_state'] == 'UNKNOWN_SCOPE'
    assert next(f for f in row['fields'] if f['id'] == str(side.pk))['content'] == original
