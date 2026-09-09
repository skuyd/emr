import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.core import signals
from django.views.debug import ExceptionReporter
import pytest

from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient
from .test_source_services import FIRST_URL, _manual


pytestmark = pytest.mark.django_db


def test_source_pages_and_original_location_keep_scope_without_external_href(django_user_model):
    client, patient, document, source = _manual(django_user_model)
    index = client.get(f'/records/{document.pk}/cloud-imaging/?patient={patient.pk}')
    assert index.status_code == 200 and '云影像来源' in index.content.decode()
    response = client.get(f'/cloud-imaging/{source.pk}/?patient={patient.pk}')
    assert response.status_code == 200
    text = response.content.decode()
    assert 'SYNTHETIC_FIRST' in text and '已确认可访问医院' not in text
    assert 'href="https://' not in text and 'src="https://' not in text
    # This private page has no external navigation. Same-origin referers keep
    # HTTPS CSRF forms usable; cross-origin requests receive no referer.
    assert 'no-store' in response['Cache-Control'] and response['Referrer-Policy'] == 'same-origin'
    viewer = response.context['viewer_url']
    query = parse_qs(urlsplit(viewer).query)
    assert query['patient'] == [str(patient.pk)] and query['cloud_evidence'] == [str(source.evidence_id)]
    shown = client.get(viewer)
    assert shown.status_code == 200 and shown.context['initial_page'] == source.evidence.document_page.page_number


def test_actual_post_confirmation_and_correction_preserve_revision_and_source(django_user_model):
    client, patient, _, source = _manual(django_user_model)
    response = client.get(f'/cloud-imaging/{source.pk}/')
    assert response.status_code == 200
    row = response.context['row']
    payload = {'patient_id': str(patient.pk), 'action': 'CONFIRM', 'expected_revision': row['revision_number'],
               'expected_source': row['source_token'], 'operation_id': str(uuid4()), 'checked_original': 'on'}
    result = client.post(f'/cloud-imaging/{source.pk}/', payload)
    assert result.status_code == 303
    source.refresh_from_db()
    assert source.status == 'CONFIRMED' and source.revision_number == 2
    stale = client.post(f'/cloud-imaging/{source.pk}/', {**payload, 'operation_id': str(uuid4())})
    assert stale.status_code == 409
    source.refresh_from_db()
    assert source.revision_number == 2


def test_viewer_can_view_results_but_cannot_submit_or_see_write_controls(django_user_model):
    _, patient, document, source = _manual(django_user_model)
    client, own = _patient(django_user_model, 'cloud-read-only')
    PatientMembership.objects.create(patient=patient, account=own.account, role='VIEWER')
    response = client.get(f'/cloud-imaging/{source.pk}/')
    assert response.status_code == 200
    assert '核对并提交' not in response.content.decode()
    assert client.post(f'/cloud-imaging/{source.pk}/', {'patient_id': patient.pk, 'action': 'CONFIRM'}).status_code == 403
    assert client.get(f'/cloud-imaging/{source.pk}/?patient={own.pk}').status_code == 404
    assert client.get(f'/records/{document.pk}/cloud-imaging/?patient={own.pk}').status_code == 404


@pytest.mark.parametrize('page_kind', ['document', 'source'])
def test_rendered_cloud_content_is_discarded_after_actual_source_change(django_user_model, monkeypatch, page_kind):
    from apps.cloud_imaging import views

    client, patient, document, source = _manual(django_user_model)
    original = views._render
    def rendering(*args, **kwargs):
        response = original(*args, **kwargs)
        type(document).objects.filter(pk=document.pk).update(material_revision=1)
        return response
    monkeypatch.setattr(views, '_render', rendering)
    url = f'/records/{document.pk}/cloud-imaging/' if page_kind == 'document' else f'/cloud-imaging/{source.pk}/'
    response = client.get(url)
    assert response.status_code == 409 and b'SYNTHETIC_FIRST' not in response.content


def test_original_location_rejects_an_obsolete_cloud_evidence_token(django_user_model):
    client, patient, document, source = _manual(django_user_model)
    response = client.get(f'/cloud-imaging/{source.pk}/')
    assert response.status_code == 200
    viewer = response.context['viewer_url']
    type(document).objects.filter(pk=document.pk).update(material_revision=1)
    assert client.get(viewer).status_code == 409


def test_original_location_rejects_non_ascii_token_without_a_server_error(django_user_model):
    client, patient, document, source = _manual(django_user_model)
    response = client.get(f'/records/{document.pk}/viewer/', {'patient': patient.pk,
        'cloud_source': source.pk, 'cloud_evidence': source.evidence_id, 'cloud_token': '无效令牌'})
    assert response.status_code == 409


@pytest.mark.parametrize('embed', [False, True])
def test_original_location_is_rechecked_after_rendering(django_user_model, monkeypatch, embed):
    from apps.documents.views import originals

    client, _, document, source = _manual(django_user_model)
    viewer = client.get(f'/cloud-imaging/{source.pk}/').context['viewer_url']
    if embed:
        viewer += '&embed=1'
    render = originals.render
    def changed(*args, **kwargs):
        response = render(*args, **kwargs)
        type(document).objects.filter(pk=document.pk).update(material_revision=1)
        return response
    monkeypatch.setattr(originals, 'render', changed)
    response = client.get(viewer)
    assert response.status_code == 409 and 'data-page-url' not in response.content.decode()


def test_https_csrf_form_and_manual_add_keep_private_fields_and_scope(django_user_model):
    from django.test import Client
    from apps.cloud_imaging.models import CloudImagingSource

    _, patient, document, _ = _manual(django_user_model)
    client = Client(enforce_csrf_checks=True)
    client.force_login(patient.account)
    path = f'/records/{document.pk}/cloud-imaging/?patient={patient.pk}'
    response = client.get(path, secure=True)
    form = response.context['manual_form']
    payload = {'action': 'ADD', 'patient_id': str(patient.pk),
        'manual-operation_id': str(form.initial['operation_id']),
        'manual-expected_source': form.initial['expected_source'], 'manual-title': 'Synthetic manual',
        'manual-url': FIRST_URL, 'manual-page_id': str(document.pages.get().pk)}
    assert client.post(path, payload, secure=True).status_code == 403
    payload['csrfmiddlewaretoken'] = client.cookies['csrftoken'].value
    added = client.post(path, payload, secure=True, HTTP_REFERER='https://testserver' + path)
    assert added.status_code == 303
    assert CloudImagingSource.objects.filter(document=document, title='Synthetic manual').get().current_url == FIRST_URL


def test_unexpected_source_failure_is_visible_without_payload_in_error_report(django_user_model, monkeypatch, settings):
    from apps.cloud_imaging import views

    settings.DEBUG = False
    client, patient, _, source = _manual(django_user_model)
    client.raise_request_exception = False
    reports = []
    def broken(*args, **kwargs):
        raise RuntimeError(FIRST_URL)
    def capture(sender, request, **kwargs):
        reports.append(ExceptionReporter(request, *sys.exc_info()).get_traceback_text())
    monkeypatch.setattr(views, '_render', broken)
    signals.got_request_exception.connect(capture, weak=False)
    try:
        response = client.get(f'/cloud-imaging/{source.pk}/')
    finally:
        signals.got_request_exception.disconnect(capture)
    assert response.status_code == 500 and len(reports) == 1
    assert 'cloud_source_request_failed' in reports[0]
    assert FIRST_URL not in reports[0] and 'SYNTHETIC_FIRST' not in reports[0]
