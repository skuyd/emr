import pytest

from apps.cancer_ordering.readmodels import resolve_ordering
from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.models import ExportJob
from apps.patients.models import PatientShare
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_services import _collect, _row, _select
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def output_post(patient, *, guard=None):
    return {'mode': 'documents', 'nickname': patient.display_name, 'sections': ['cancer_ordering'],
            'cancer_candidate_ids': [_row(patient)['id']], 'include_indicator_ordering': 'on',
            'cancer_expected_fingerprint': guard or resolve_ordering(patient)['fingerprint'], 'action': 'preview'}


@pytest.mark.parametrize('kind', ['export', 'share'])
def test_actual_form_requires_explicit_content_and_preserves_stale_submitted_guard(django_user_model, kind):
    client, patient = _patient(django_user_model, 'cancer-output-form-' + kind)
    _collect(patient)
    path = '/visit/' if kind == 'export' else f'/patients/{patient.pk}/shares/'
    response = client.get(path)
    assert response.status_code == 200
    form = response.context['form']
    assert form['cancer_candidate_ids'].value() in (None, [])
    assert not form['include_indicator_ordering'].value()
    expected = form['cancer_expected_fingerprint'].value()
    assert expected == resolve_ordering(patient)['fingerprint']
    _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
    response = client.post(path, output_post(patient, guard=expected))
    assert response.status_code == 400
    assert response.context['form']['cancer_expected_fingerprint'].value() == expected
    assert not ExportJob.objects.filter(patient=patient).exists() and not PatientShare.objects.filter(patient=patient).exists()
    response = client.post(path, output_post(patient))
    assert response.status_code == (302 if kind == 'export' else 201)
    record = (ExportJob if kind == 'export' else PatientShare).objects.get(patient=patient)
    assert record.snapshot['cancer_candidates'][0]['id'] == _row(patient)['id']
    assert record.snapshot['indicator_ordering'][0]['profile'] == 'PANCREAS'
    assert record.snapshot['documents'] == []


@pytest.mark.parametrize('kind', ['export', 'share'])
@pytest.mark.parametrize('method', ['get', 'invalid_post'])
def test_initial_form_state_is_rechecked_after_real_template_render(django_user_model, monkeypatch, kind, method):
    from apps.exports import views
    from apps.patients import share_views
    client, patient = _patient(django_user_model, 'cancer-output-render-' + kind + method)
    _collect(patient)
    path = '/visit/' if kind == 'export' else f'/patients/{patient.pk}/shares/'
    module = views if kind == 'export' else share_views
    original = module.render
    def change_after_render(*args, **kwargs):
        response = original(*args, **kwargs)
        parsed_facts(patient, ['出院诊断：胰腺癌。'])
        return response
    monkeypatch.setattr(module, 'render', change_after_render)
    response = client.get(path) if method == 'get' else client.post(path, {})
    assert response.status_code == 409
    assert _row(patient)['id'] not in response.content.decode() and '肺癌' not in response.content.decode()


@pytest.mark.parametrize('bad', ['非英文指纹', None, ['a'], {'x': 1}, 'a' * 65])
def test_malformed_body_guard_is_a_selection_error_and_never_a_server_exception(django_user_model, bad):
    _, patient = _patient(django_user_model, 'cancer-output-malformed')
    _collect(patient)
    with pytest.raises(ExportInputError):
        build_snapshot(patient, selected_body(patient, cancer_candidate_ids=[_row(patient)['id']], cancer_expected_fingerprint=bad))
