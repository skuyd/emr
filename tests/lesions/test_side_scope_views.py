from copy import deepcopy

from django.test import Client
import pytest

from apps.lesions.models import Lesion, LesionMatchProposal
from apps.lesions.proposals import propose_matches
from apps.lesions.services import generate_proposals
from tests.lesions.test_proposal_decisions import pair
from tests.lesions.test_side_scope_consumption import scoped


def test_consumer_conflict_cannot_supply_a_residual_whole_side_reason():
    first, second = scoped('first'), scoped('second')
    original = first['fields'][1]
    original['conflict'] = True
    residual = deepcopy(original)
    residual.update(id='another-whole-field', conflict=False)
    first['fields'].append(residual)
    proposal = propose_matches([first, second])[0]
    assert 'explicit_side_equal' not in {row['code'] for row in proposal.reasons}
    assert 'field_conflict' in proposal.blockers


@pytest.mark.django_db
def test_outdated_proposal_shows_old_reasons_as_history_and_cannot_be_accepted_by_post(django_user_model):
    patient = pair(django_user_model, 'scope-rule-ui')
    generate_proposals(patient, actor=patient.account)
    proposal = LesionMatchProposal.objects.get()
    LesionMatchProposal.objects.filter(pk=proposal.pk).update(rule_version='explicit_location_candidates_v1')
    client = Client()
    client.force_login(patient.account)
    url = f'/lesions/proposals/{proposal.pk}/'
    page = client.get(url, {'patient': str(patient.pk)})
    assert page.status_code == 200
    assert '当时保存的原提议依据（旧规则）' in page.content.decode()
    assert 'value="CONFIRM"' not in page.content.decode()
    form = page.context['form']
    payload = {name: form[name].value() for name in form.fields}
    payload.update(action='CONFIRM', patient_id=str(patient.pk), checked_original='on', name='合成名称')
    response = client.post(url, payload)
    assert response.status_code == 409
    assert '规则' in response.content.decode()
    assert 'value="CONFIRM"' not in response.content.decode()
    assert not Lesion.objects.exists()
