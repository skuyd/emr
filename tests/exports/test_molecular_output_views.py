import io
import json
import pytest
from pypdf import PdfReader

from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from tests.documents.fakes import InMemoryObjectStore
from tests.exports.test_molecular_exports import ready_graph, selection
from tests.facts.pathology_factories import review

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('destination', ['export', 'share'])
@pytest.mark.parametrize('post', [False, True])
def test_selection_permission_is_rechecked_after_final_molecular_source_check(django_user_model, monkeypatch, destination, post):
    from apps.exports import molecular
    client, patient, _, _, _ = ready_graph(django_user_model, 'molecular-last-access-' + destination + str(post))
    original = molecular.selection_unchanged
    revoked = []
    def after_source(*args):
        valid = original(*args)
        assert valid
        django_user_model.objects.filter(pk=patient.account_id).update(is_active=False)
        revoked.append(True)
        return valid
    monkeypatch.setattr(molecular, 'selection_unchanged', after_source)
    url = '/visit/' if destination == 'export' else f'/patients/{patient.pk}/shares/'
    response = client.post(url, {}) if post else client.get(url)
    assert revoked == [True] and response.status_code == 403
    assert 'NM_SYN.2' not in response.content.decode()


@pytest.mark.parametrize('destination', ['export', 'share'])
@pytest.mark.parametrize('post', [False, True])
def test_choice_final_render_rechecks_full_molecular_identity(django_user_model, monkeypatch, destination, post):
    client, patient, _, _, fields = ready_graph(django_user_model, 'molecular-render-' + destination + str(post))
    if destination == 'export':
        from apps.exports import views
        path, original, url = 'apps.exports.views._render', views._render, '/visit/'
    else:
        from apps.patients import share_views
        path, original, url = 'apps.patients.share_views.render', share_views.render, f'/patients/{patient.pk}/shares/'
    changed = []
    def during(*args, **kwargs):
        response = original(*args, **kwargs)
        if not changed:
            assert 'NM_SYN.2' in response.content.decode()
            changed.append(True)
            review(patient, fields['identity'], 'EXCLUDE')
        return response
    monkeypatch.setattr(path, during)
    response = client.post(url, {}) if post else client.get(url)
    assert changed == [True] and response.status_code == 409
    assert 'NM_SYN.2' not in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_actual_http_preview_pdf_generation_and_download(django_user_model, monkeypatch):
    client, patient, document, _, fields = ready_graph(django_user_model, 'molecular-output-http')
    assert client.get('/visit/').status_code == 200
    response = client.post('/visit/', {**selection(document, fields['metric']), 'nickname': '合成患者',
        'custom_clinical_fields': 'on', 'action': 'preview', 'details': 'on'})
    assert response.status_code == 302
    url = response['Location']
    page = client.get(url)
    assert page.status_code == 200 and 'NM_SYN.2' in page.content.decode()
    response = client.get(url + 'pdf/')
    assert response.status_code == 200
    text = ''.join(p.extract_text() for p in PdfReader(io.BytesIO(b''.join(response.streaming_content))).pages)
    response.close()
    assert '01.20' in text and 'NM_SYN.2' in text and '检测甲' not in text
    monkeypatch.setattr('apps.exports.views.safe_enqueue_export', lambda _: None)
    assert client.post(url, {'format': 'json'}).status_code == 302
    job = ExportJob.objects.get(patient=patient); store = InMemoryObjectStore()
    generate_export(job.pk, store)
    monkeypatch.setattr('apps.exports.views.get_object_store', lambda: store)
    response = client.get(url + 'download/')
    assert response.status_code == 200
    data = json.loads(b''.join(response.streaming_content)); response.close()
    assert data['clinical_fields'][0]['content']['molecular_semantic_unit']['policy'] == 'MOLECULAR_SEMANTIC_UNIT_V1'
    review(patient, fields['identity'], 'REVOKE')
    assert client.get(url).status_code == 409 and client.get(url + 'download/').status_code == 409
