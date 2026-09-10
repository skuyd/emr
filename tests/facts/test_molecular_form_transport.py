import pytest

from tests.facts.test_molecular_pipeline import fixture, report_rows
from tests.facts.test_pathology_views import submitted

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('changed', [False, True])
def test_browser_crlf_transport_does_not_change_actual_ocr_source_or_block_confirmation(django_user_model, changed):
    from django.test import Client
    from apps.facts.clinical_readmodels import effective_field
    patient, document, _, _ = fixture(django_user_model, report_rows())
    client = Client()
    client.force_login(patient.account)
    fact = document.facts.get(field_key='variant.identity')
    before = fact.automatic_content
    assert '\n' in before['raw_value']
    url = f'/facts/{fact.pk}/'
    response = client.get(url)
    data = submitted(response.context['form'])
    data['raw_value'] = data['raw_value'].replace('\n', '\r\n')
    if changed:
        data['raw_value'] += ' changed'
    data.update(patient_id=str(patient.pk), action='CONFIRM', checked_original='on')
    response = client.post(url, data)
    if changed:
        assert response.status_code == 400
        assert not fact.revisions.exists()
        fact.refresh_from_db()
        assert fact.automatic_content == before
        return
    assert response.status_code == 302, response.context.get('error')
    fact.refresh_from_db()
    assert fact.automatic_content == before
    assert effective_field(fact)['content']['raw_value'] == before['raw_value']
    assert fact.revisions.get().after['content'] == before
