"""Ordinary field review sends the explicit parent expectation from its form."""
import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_scoped_laterality import confirm


pytestmark = pytest.mark.django_db


def form_data(response):
    form = response.context['form']
    return {name: form[name].value() if form[name].value() is not None else '' for name in form.fields}


def test_scope_service_does_not_accept_implicit_or_forged_parent_expectation(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'scope-explicit-parent-api')
    confirm(patient, parent)
    child_row = effective_fact(child)
    args = dict(actor=patient.account, action='CONFIRM', expected_revision=0,
                expected_source=child_row['current_source_token'], checked_original=True)
    with pytest.raises(ValidationError):
        revise_fact(patient, child.pk, **args)
    with pytest.raises(FactConflict):
        revise_fact(patient, child.pk, **args, expected_parent_revision=parent.revision_number,
                    expected_parent_source='different-parent')
    assert child.revisions.count() == 0


def test_actual_scoped_field_page_reviews_named_members_and_submits_both_heads(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'scope-member-field-page')
    confirm(patient, parent)
    client = Client()
    client.force_login(patient.account)
    url = f'/facts/{child.pk}/?patient={patient.pk}'
    response = client.get(url)
    assert response.status_code == 200
    html = response.content.decode()
    assert 'name="member_1_code"' in html and 'name="size_1"' not in html
    assert '仅限列明部位' in html and '父位置' in html and '纵隔及双肺门' in html
    values = form_data(response)
    parent.refresh_from_db()
    assert values['expected_parent_revision'] == parent.revision_number
    assert values['expected_parent_source'] == effective_fact(parent)['current_source_token']
    values.update(patient_id=str(patient.pk), action='CONFIRM', checked_original='on')
    accepted = client.post(url, values)
    assert accepted.status_code == 302
    child.refresh_from_db()
    assert effective_fact(child)['usable']
    assert effective_fact(child)['content']['value']['members'][0]['site_text'] == '双肺门'


def test_tampering_only_parent_token_in_actual_review_is_rejected(django_user_model):
    patient, _, parent, child = fixture(django_user_model, 'scope-parent-form-tamper')
    confirm(patient, parent)
    client = Client()
    client.force_login(patient.account)
    url = f'/facts/{child.pk}/?patient={patient.pk}'
    values = form_data(client.get(url))
    values.update(patient_id=str(patient.pk), action='CONFIRM', checked_original='on', expected_parent_source='not-current')
    response = client.post(url, values)
    assert response.status_code == 409
    assert child.revisions.count() == 0
