"""Only current confirmed private sources can leave the application explicitly."""
import sys
from contextlib import contextmanager
from io import StringIO
import logging

from django.core import signals
from django.test import Client
from django.views.debug import get_exception_reporter_class
import pytest

from apps.cloud_imaging.readmodels import source_details
from apps.operations.audit import _hash
from apps.operations.models import AuditEvent
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient
from .test_source_services import FIRST_URL, SECOND_URL, _manual, _decide


pytestmark = pytest.mark.django_db


@contextmanager
def application_logs():
    # Capture the actual configured Django sink, including its production
    # redaction filter. django.propagate=False bypasses pytest's root caplog.
    handlers = [handler for handler in logging.getLogger('django').handlers
                if isinstance(handler, logging.StreamHandler)]
    assert handlers
    output = StringIO()
    originals = [(handler, handler.stream) for handler in handlers]
    for handler in handlers:
        handler.setStream(output)
    try:
        yield output
    finally:
        for handler, original in originals:
            handler.setStream(original)


def confirmed(django_user_model):
    client, patient, document, source = _manual(django_user_model)
    return client, patient, document, _decide(patient, source, 'CONFIRM')


def payload(patient, source):
    row = source_details(patient, actor=patient.account, source_id=source.pk)
    return {'patient_id': str(patient.pk), 'expected_source': row['source_token'],
            'expected_revision': row['revision_number']}


def test_notice_contains_only_safe_site_and_internal_scope_and_does_not_open(django_user_model):
    client, patient, _, source = confirmed(django_user_model)
    source = _decide(patient, source, 'CORRECT', changes={'title': FIRST_URL})
    response = client.get(f'/cloud-imaging/{source.pk}/visit/')
    assert response.status_code == 200
    html = response.content.decode()
    assert 'images.example.invalid' in html and '外部站点' in html
    assert FIRST_URL not in html and 'SYNTHETIC_FIRST' not in html
    assert 'href="https://' not in html and 'data-url=' not in html
    assert f'/cloud-imaging/{source.pk}/open/' in html
    assert 'target="_blank"' in html and 'rel="noopener noreferrer"' in html
    assert response['Referrer-Policy'] == 'no-referrer'
    assert 'no-store' in response['Cache-Control'] and 'private' in response['Cache-Control']
    assert 'Location' not in response
    assert source.revisions.count() == 3


@pytest.mark.parametrize('scripted', [False, True])
def test_explicit_post_preserves_target_and_records_one_open_initiated_event(django_user_model, scripted):
    client, patient, _, source = confirmed(django_user_model)
    before = AuditEvent.objects.count()
    response = client.post(f'/cloud-imaging/{source.pk}/open/', payload(patient, source),
                           **({'HTTP_X_CLOUD_OPEN': 'navigate'} if scripted else {}))
    assert response.status_code == (200 if scripted else 303) and response['Location'] == FIRST_URL
    assert response['Referrer-Policy'] == 'no-referrer' and 'no-store' in response['Cache-Control']
    assert not response.content
    events = list(AuditEvent.objects.order_by('created_at')[before:])
    assert len(events) == 1
    event = events[0]
    assert (event.action, event.result, event.reason_code) == ('cloud_source_open_initiated', 'succeeded', '')
    assert event.actor_hash == _hash('actor', patient.account_id)
    assert event.target_hash == _hash('target', source.pk)
    assert event.patient_hash == _hash('patient', patient.pk)
    assert event.route_name == 'cloud_imaging:open' and event.request_id
    assert 'SYNTHETIC_FIRST' not in str(event.__dict__)


@pytest.mark.parametrize('state', ['PENDING', 'EXCLUDED', 'UNDO', 'CHANGED'])
def test_unconfirmed_or_changed_source_cannot_offer_or_redirect(django_user_model, state):
    client, patient, document, source = _manual(django_user_model)
    old = payload(patient, source)
    if state != 'PENDING':
        source = _decide(patient, source, 'CONFIRM')
        old = payload(patient, source)
        if state == 'EXCLUDED':
            _decide(patient, source, 'EXCLUDE')
        elif state == 'UNDO':
            _decide(patient, source, 'UNDO')
        else:
            type(document).objects.filter(pk=document.pk).update(material_revision=1)
    for response in [client.get(f'/cloud-imaging/{source.pk}/visit/'),
                     client.post(f'/cloud-imaging/{source.pk}/open/', old)]:
        assert response.status_code == 409 and 'Location' not in response
        assert 'SYNTHETIC_FIRST' not in response.content.decode()


def test_viewer_can_open_but_foreign_scope_and_revoked_member_cannot(django_user_model):
    _, patient, _, source = confirmed(django_user_model)
    reader, own = _patient(django_user_model, 'cloud-open-reader')
    member = PatientMembership.objects.create(patient=patient, account=own.account, role='VIEWER')
    assert reader.get(f'/cloud-imaging/{source.pk}/visit/').status_code == 200
    response = reader.post(f'/cloud-imaging/{source.pk}/open/', payload(patient, source))
    assert response.status_code == 303 and response['Location'] == FIRST_URL
    assert reader.get(f'/cloud-imaging/{source.pk}/visit/?patient={own.pk}').status_code == 404
    assert reader.post(f'/cloud-imaging/{source.pk}/open/', {**payload(patient, source), 'patient_id': own.pk}).status_code == 404
    member.delete()
    assert reader.post(f'/cloud-imaging/{source.pk}/open/', payload(patient, source)).status_code == 404


@pytest.mark.parametrize('bad', [{'url': SECOND_URL}, {'next': SECOND_URL},
    {'expected_source': FIRST_URL}, {'expected_revision': -1}, {'expected_source': '0' * 64}])
def test_client_cannot_supply_a_target_or_echo_sensitive_input(django_user_model, bad):
    client, patient, _, source = confirmed(django_user_model)
    response = client.post(f'/cloud-imaging/{source.pk}/open/', {**payload(patient, source), **bad})
    assert response.status_code in {400, 409} and 'Location' not in response
    assert 'SYNTHETIC_FIRST' not in response.content.decode()
    assert 'SYNTHETIC_CORRECTION' not in response.content.decode()


@pytest.mark.parametrize('phase', ['notice', 'redirect', 'resolver', 'query'])
def test_failed_open_keeps_audit_and_exception_identity_without_access_payload(
        django_user_model, monkeypatch, settings, phase):
    from apps.cloud_imaging import views

    settings.DEBUG = False
    client, patient, _, source = confirmed(django_user_model)
    client.raise_request_exception = False
    reports = []
    def broken(*args, **kwargs):
        raise RuntimeError(FIRST_URL)
    def capture(sender, request, **kwargs):
        reports.append(get_exception_reporter_class(request)(request, *sys.exc_info()).get_traceback_text())
    seam = '_render' if phase in {'notice', 'query'} else '_external_redirect' if phase == 'redirect' else 'open_source'
    monkeypatch.setattr(views, seam, broken)
    signals.got_request_exception.connect(capture, weak=False)
    try:
        with application_logs() as output:
            response = (client.get(f'/cloud-imaging/{source.pk}/visit/', {'url': FIRST_URL} if phase == 'query' else {})
                        if phase in {'notice', 'query'} else client.post(f'/cloud-imaging/{source.pk}/open/', payload(patient, source)))
    finally:
        signals.got_request_exception.disconnect(capture)
    assert response.status_code == 500 and len(reports) == 1
    assert 'cloud_source_request_failed' in reports[0]
    assert 'SYNTHETIC_FIRST' not in reports[0] and FIRST_URL not in reports[0]
    assert 'Internal Server Error' in output.getvalue()
    assert 'SYNTHETIC_FIRST' not in output.getvalue()
    event = AuditEvent.objects.order_by('-created_at').first()
    assert event.result == 'failed' and event.reason_code == 'server_error' and event.request_id
    assert response['Referrer-Policy'] == 'no-referrer' and 'Location' not in response


def test_get_open_has_no_target_and_post_requires_real_csrf(django_user_model):
    _, patient, _, source = confirmed(django_user_model)
    client = Client(enforce_csrf_checks=True)
    client.force_login(patient.account)
    path = f'/cloud-imaging/{source.pk}/open/'
    assert client.get(path).status_code == 405
    assert client.post(path, payload(patient, source)).status_code == 403
    notice = client.get(f'/cloud-imaging/{source.pk}/visit/', secure=True)
    assert notice.status_code == 200
    response = client.post(path, {**payload(patient, source),
        'csrfmiddlewaretoken': client.cookies['csrftoken'].value}, secure=True,
        HTTP_ORIGIN='https://testserver')
    assert response.status_code == 303 and response['Location'] == FIRST_URL


def test_old_notice_never_opens_a_later_confirmed_target(django_user_model):
    client, patient, _, source = confirmed(django_user_model)
    old = payload(patient, source)
    _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL})
    response = client.post(f'/cloud-imaging/{source.pk}/open/', old)
    assert response.status_code == 409 and 'Location' not in response


def test_malformed_origin_is_denied_without_logging_its_embedded_access_path(django_user_model):
    _, patient, _, source = confirmed(django_user_model)
    client = Client(enforce_csrf_checks=True)
    client.force_login(patient.account)
    client.get(f'/cloud-imaging/{source.pk}/visit/', secure=True)
    with application_logs() as output:
        response = client.post(f'/cloud-imaging/{source.pk}/open/', {**payload(patient, source),
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value}, secure=True,
            HTTP_ORIGIN='https://images.example.invalid/SYNTHETIC_PRIVATE_PATH')
    assert response.status_code == 403 and 'Location' not in response
    assert 'Forbidden' in output.getvalue()
    assert 'SYNTHETIC_PRIVATE_PATH' not in output.getvalue()
    assert response['Referrer-Policy'] == 'no-referrer'


@pytest.mark.parametrize('phase', ['notice', 'redirect'])
def test_source_change_after_response_construction_discards_the_whole_response(django_user_model, monkeypatch, phase):
    from apps.cloud_imaging import views

    client, patient, _, source = confirmed(django_user_model)
    data = payload(patient, source)
    seam = '_render' if phase == 'notice' else '_external_redirect'
    original = getattr(views, seam)
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL})
        return result
    monkeypatch.setattr(views, seam, changed)
    response = (client.get(f'/cloud-imaging/{source.pk}/visit/') if phase == 'notice'
                else client.post(f'/cloud-imaging/{source.pk}/open/', data))
    assert response.status_code == 409 and 'Location' not in response
    assert 'SYNTHETIC_FIRST' not in response.content.decode()
    assert 'SYNTHETIC_CORRECTION' not in response.content.decode()
