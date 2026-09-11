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


@pytest.mark.parametrize('case', ['text_whitespace', 'component_whitespace', 'embedded_component_newline'])
@pytest.mark.parametrize('changed', [False, True])
def test_unchanged_original_whitespace_and_list_structure_survive_http_confirmation(django_user_model, case, changed):
    from tests.facts.molecular_factories import graph, add, variant_source
    from tests.facts.test_molecular_contracts import variant
    client, patient, _, report, fields = graph(django_user_model)
    targets = {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}
    if case == 'text_whitespace':
        field = add(patient, report, 'assay.panel_name', 'assay:a', {'text': '  SYN panel  \n第二行 '}, targets)
        control = 'text_value'
    else:
        value = variant()
        value['transcripts']['values'] = [' NM_SYN.2 ', 'NM_SYN.3'] if case == 'component_whitespace' else ['NM_SYN.2\nNM_SYN.3']
        field = add(patient, report, 'variant.identity', 'variant:roundtrip', value, targets,
            raw='标本甲；检测甲；' + variant_source() + '；NM_SYN.2\nNM_SYN.3')
        control = 'transcripts_raw'
    before = field.automatic_content
    url = f'/facts/{field.pk}/'
    response = client.get(url)
    assert response.status_code == 200
    data = submitted(response.context['form'])
    for key, value in data.items():
        if isinstance(value, str):
            data[key] = value.replace('\n', '\r\n')
    if changed:
        data[control] += ' changed'
    data.update(patient_id=str(patient.pk), action='CONFIRM', checked_original='on')
    response = client.post(url, data)
    field.refresh_from_db()
    assert field.automatic_content == before
    if changed:
        assert response.status_code == 400
        assert not field.revisions.exists()
    else:
        assert response.status_code == 302, response.context.get('error')
        assert field.revisions.get().after['content'] == before
