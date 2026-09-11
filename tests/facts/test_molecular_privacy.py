import sys

import pytest
from django.core import signals
from django.views.debug import get_exception_reporter_class

from tests.facts.molecular_factories import graph
from tests.cloud_imaging.test_controlled_open import application_logs

pytestmark = pytest.mark.django_db
PRIVATE = "SYNTHETIC_MOLECULAR_RAW_NOT_FOR_DIAGNOSTICS"


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("explicit", [False, True])
def test_actual_early_fact_failure_keeps_originals_out_of_error_chain_and_logs(django_user_model, settings, monkeypatch, method, explicit):
    from apps.core import decorators
    settings.DEBUG = False
    client, patient, _, report, _ = graph(django_user_model)
    client.raise_request_exception = False
    reports, errors = [], []
    def fail(*args, **kwargs):
        original_source = PRIVATE
        try:
            raise ValueError(original_source)
        except ValueError as cause:
            error = RuntimeError(original_source)
            errors.extend([cause, error])
            if explicit:
                raise error from cause
            raise error
    def capture(sender, request, **kwargs):
        reporter = get_exception_reporter_class(request)(request, *sys.exc_info())
        reports.append((reporter.get_traceback_data(), reporter.get_traceback_text(), reporter.get_traceback_html()))
    monkeypatch.setattr(decorators, "get_request_patient", fail)
    signals.got_request_exception.connect(capture, weak=False)
    try:
        with application_logs() as output:
            response = getattr(client, method)(f"/facts/reports/{report.pk}/", {"raw_value": PRIVATE, "source": PRIVATE, "patient_id": str(patient.pk)})
    finally:
        signals.got_request_exception.disconnect(capture)
    assert response.status_code == 500 and len(reports) == 1
    data, plain, html = reports[0]
    assert data["exception_type"] == "RuntimeError"
    for body in [plain, html, repr(data), output.getvalue()]:
        assert PRIVATE not in body
    assert data["request_meta"]["route_name"] == "facts:report"
    assert data["request_meta"]["request_id"]
    assert "ValueError" in plain and "RuntimeError" in plain
    assert ("was the direct cause" if explicit else "During handling") in plain
    assert errors[1].__context__ is errors[0] and str(errors[0]) == PRIVATE
    assert errors[1].__cause__ is (errors[0] if explicit else None)
    assert response["Referrer-Policy"] == "same-origin"
    assert "no-store" in response["Cache-Control"]
