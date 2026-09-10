import sys

from django.core import signals
from django.views.debug import get_exception_reporter_class
import pytest

from apps.cloud_imaging import shared_views
from apps.exports import views as export_views
from apps.patients.sharing import create_share
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_shares import exchange
from .test_controlled_open import application_logs, confirmed
from .test_selected_output import source_selection
from .test_selected_output_bindings import job_for
from .test_source_services import FIRST_URL

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('route', ['export_prepare','export_preview','shared_visit'])
def test_selected_output_actual_failure_report_never_serializes_target(django_user_model,monkeypatch,settings,route):
    settings.DEBUG=False
    client,patient,_,source=confirmed(django_user_model)
    if route=='shared_visit':
        client,_=_patient(django_user_model,'selected-error-reader')
        created=create_share(patient,patient.account,source_selection(source,sections=[]))
        share_id=exchange(client,created.token)
        target=shared_views
        path=f'/shared/{share_id}/cloud-imaging/{source.pk}/visit/'
    else:
        target=export_views
        path='/visit/' if route=='export_prepare' else f'/visit/{job_for(client,patient,source).pk}/'
    def failing(*args,**kwargs):
        try:raise ValueError(FIRST_URL)
        except ValueError as cause:raise RuntimeError('synthetic_output_render_failed') from cause
    monkeypatch.setattr(target,'render',failing)
    client.raise_request_exception=False
    captured=[]
    def capture(sender,request,**kwargs):
        reporter=get_exception_reporter_class(request)(request,*sys.exc_info())
        captured.append((reporter.get_traceback_text(),reporter.get_traceback_html()))
    signals.got_request_exception.connect(capture,weak=False)
    try:
        with application_logs() as logs:response=client.get(path)
    finally:signals.got_request_exception.disconnect(capture)
    assert response.status_code==500 and len(captured)==1
    for output in (*captured[0],logs.getvalue()):
        assert FIRST_URL not in output and 'SYNTHETIC_FIRST' not in output
    assert 'ValueError' in captured[0][0] and 'RuntimeError' in captured[0][0]
    assert 'Location' not in response and response['Referrer-Policy']=='no-referrer'
