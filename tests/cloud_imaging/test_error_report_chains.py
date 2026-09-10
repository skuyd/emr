"""Private diagnostics preserve chain structure without exception messages."""
import sys

from django.core import signals
from django.test import RequestFactory
from django.urls import resolve
from django.views.debug import get_exception_reporter_class
import pytest

from apps.cloud_imaging.privacy import CloudOpenExceptionReporter
from apps.operations.models import AuditEvent
from .test_controlled_open import application_logs, confirmed, payload
from .test_source_services import FIRST_URL, SECOND_URL


def _failing_chain(explicit, observed):
    try:
        raise ValueError(FIRST_URL)
    except ValueError as original:
        observed.append(original)
        failure = RuntimeError('source_resolution_failed')
        observed.append(failure)
        if explicit:
            raise failure from original
        raise failure


@pytest.mark.django_db
@pytest.mark.parametrize('route', ['visit', 'open'])
@pytest.mark.parametrize('chain', ['explicit', 'implicit', 'view_wrapped'])
def test_actual_early_error_reports_and_configured_logs_keep_chains_private(
        django_user_model, monkeypatch, settings, route, chain):
    from apps.cloud_imaging import views
    from apps.core import decorators

    settings.DEBUG = False
    client, patient, _, source = confirmed(django_user_model)
    posted = payload(patient, source)
    client.raise_request_exception = False
    reports, observed = [], []

    def failing(*args, **kwargs):
        _failing_chain(chain == 'explicit', observed)

    def capture(sender, request, **kwargs):
        reporter = get_exception_reporter_class(request)(request, *sys.exc_info())
        reports.append((reporter.get_traceback_data(), reporter.get_traceback_text(),
                        reporter.get_traceback_html()))

    if chain == 'view_wrapped':
        monkeypatch.setattr(views, 'visit_source', failing)
    else:
        monkeypatch.setattr(decorators, 'get_request_patient', failing)
    signals.got_request_exception.connect(capture, weak=False)
    try:
        with application_logs() as output:
            path = f'/cloud-imaging/{source.pk}/{route}/'
            response = client.get(path) if route == 'visit' else client.post(path, posted)
    finally:
        signals.got_request_exception.disconnect(capture)

    assert response.status_code == 500 and len(reports) == 1
    data, plain, html = reports[0]
    assert data['exception_type'] == 'RuntimeError'
    assert data['request_meta']['route_name'] == f'cloud_imaging:{route}'
    assert data['request_meta']['request_id']
    expected_function = 'wrapped' if chain == 'view_wrapped' else 'failing'
    assert any(frame.get('function') == expected_function for frame in data['frames'])
    assert 'cloud_source_request_failed' in plain and 'cloud_source_request_failed' in html
    for rendered in (plain, html, repr(data)):
        assert FIRST_URL not in rendered and 'SYNTHETIC_FIRST' not in rendered
    assert all(not isinstance(frame.get(key), BaseException)
               for frame in data['frames'] for key in ('exc_cause', 'exc_cause_explicit'))
    if chain != 'view_wrapped':
        assert 'ValueError' in plain
        assert ('was the direct cause' if chain == 'explicit' else 'During handling') in plain

    # Projection must not mutate or suppress the actual failure's cause/context.
    original, failure = observed
    assert str(original) == FIRST_URL
    assert failure.__context__ is original
    assert failure.__cause__ is (original if chain == 'explicit' else None)
    assert failure.__suppress_context__ is (chain == 'explicit')
    assert response['Referrer-Policy'] == 'no-referrer' and 'Location' not in response
    assert 'no-store' in response['Cache-Control']
    assert 'Internal Server Error' in output.getvalue()
    assert 'SYNTHETIC_FIRST' not in output.getvalue()
    event = AuditEvent.objects.order_by('-created_at').first()
    assert event.result == 'failed' and event.reason_code == 'server_error'
    assert str(event.request_id) == data['request_meta']['request_id']


def _nested_chain(explicit, leaf_has_traceback, observed):
    first = ValueError(FIRST_URL)
    try:
        if leaf_has_traceback:
            raise first
    except ValueError:
        pass
    observed.append(first)
    try:
        second = ValueError(SECOND_URL)
        observed.append(second)
        if explicit:
            raise second from first
        second.__context__ = first
        raise second
    except ValueError as previous:
        final = RuntimeError('stable_outer_failure')
        observed.append(final)
        if explicit:
            raise final from previous
        raise final


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('leaf_has_traceback', [False, True])
def test_report_projection_keeps_distinct_same_class_causes_and_missing_tracebacks(
        settings, explicit, leaf_has_traceback):
    settings.DEBUG = False
    request = RequestFactory().get('/cloud-imaging/11111111-1111-1111-1111-111111111111/visit/')
    request.resolver_match = resolve(request.path)
    observed = []
    try:
        _nested_chain(explicit, leaf_has_traceback, observed)
    except RuntimeError:
        reporter = CloudOpenExceptionReporter(request, *sys.exc_info())
        data = reporter.get_traceback_data()
        plain, html = reporter.get_traceback_text(), reporter.get_traceback_html()
    for rendered in (plain, html, repr(data)):
        assert FIRST_URL not in rendered and SECOND_URL not in rendered
        assert 'SYNTHETIC_FIRST' not in rendered and 'SYNTHETIC_CORRECTION' not in rendered
    causes = {str(frame['exc_cause']) for frame in data['frames'] if frame.get('exc_cause')}
    assert len(causes) == 2 and all('ValueError' in cause for cause in causes)
    transition = 'was the direct cause' if explicit else 'During handling'
    assert plain.count(transition) == 2
    assert any(frame.get('function') == '_nested_chain' and frame.get('lineno') for frame in data['frames'])
    assert data['exception_type'] == 'RuntimeError'
    assert all(type(frame['exc_cause_explicit']) is bool for frame in data['frames'])
    first, second, final = observed
    assert bool(first.__traceback__) is leaf_has_traceback
    assert str(first) == FIRST_URL and str(second) == SECOND_URL
    assert (second.__cause__ if explicit else second.__context__) is first
    assert (final.__cause__ if explicit else final.__context__) is second
