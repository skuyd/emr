"""Error forms must retain the identity of the options they were built from."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from django.db import connection
import pytest

from apps.cloud_imaging import forms
from apps.cloud_imaging.readmodels import document_snapshot
from apps.cloud_imaging.services import add_manual_source
from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import revise_report
from tests.facts.test_clinical_foundation import clinical_fixture
from tests.integration.test_family_postgres_concurrency import thread_call


def invalid_request(django_user_model, page_kind):
    client, patient, document, _, _ = clinical_fixture(
        django_user_model, name='cloud-error-form-' + page_kind)
    report = document.clinical_reports.get()
    material = document_snapshot(patient, actor=patient.account, document_id=document.pk)
    source = add_manual_source(patient, actor=patient.account, document_id=document.pk,
        page_id=document.pages.first().pk, url='https://synthetic.invalid/cloud-source',
        expected_source=material['input_token'], operation_id=uuid4())
    path = (f'/records/{document.pk}/cloud-imaging/' if page_kind == 'document'
            else f'/cloud-imaging/{source.pk}/')
    response = client.get(path)
    assert response.status_code == 200
    form = response.context['manual_form' if page_kind == 'document' else 'form']
    prefix = 'manual-' if page_kind == 'document' else ''
    payload = {'patient_id': str(patient.pk), 'action': 'ADD' if page_kind == 'document' else 'INVALID',
        prefix + 'operation_id': str(form.initial['operation_id']),
        prefix + 'expected_source': form.initial['expected_source'],
        prefix + 'page_id': str(document.pages.first().pk)}
    if page_kind == 'source':
        payload['expected_revision'] = form.initial['expected_revision']
    form_type = forms.ManualSourceForm if page_kind == 'document' else forms.DecisionForm
    return client, patient, document, report, path, payload, form_type


def exclude_report(patient, report):
    report.refresh_from_db()
    revise_report(patient, actor=patient.account, report_id=report.pk, action='EXCLUDE',
        expected_revision=report.revision_number, expected_source=report_source_token(report))


def assert_discarded(response, patient, document, report):
    assert document_snapshot(patient, actor=patient.account, document_id=document.pk)['reports'] == []
    assert response.status_code == 409
    html = response.content.decode()
    assert f'<option value="{report.pk}"' not in html
    assert '<form' not in html and 'synthetic.invalid' not in html


@pytest.mark.django_db
@pytest.mark.parametrize('page_kind', ['document', 'source'])
@pytest.mark.parametrize('change', [True, False])
def test_invalid_post_checks_the_snapshot_used_to_build_report_choices(
        django_user_model, monkeypatch, page_kind, change):
    client, patient, document, report, path, payload, form_type = invalid_request(django_user_model, page_kind)
    original = form_type.is_valid
    calls = []

    def validate(self):
        valid = original(self)
        assert not valid
        if change and not calls:
            calls.append(True)
            exclude_report(patient, report)
        return valid

    monkeypatch.setattr(form_type, 'is_valid', validate)
    response = client.post(path, payload)
    if change:
        assert calls == [True]
        assert_discarded(response, patient, document, report)
    else:
        assert response.status_code == 400
        assert f'<option value="{report.pk}"' in response.content.decode()


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('page_kind', ['document', 'source'])
def test_invalid_post_discards_old_options_after_an_independent_report_commit(
        django_user_model, monkeypatch, page_kind):
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the dedicated PostgreSQL cloud-source test database')
    client, patient, document, report, path, payload, form_type = invalid_request(django_user_model, page_kind)
    original = form_type.is_valid
    entered, resume = Event(), Event()

    def validate(self):
        valid = original(self)
        assert not valid
        entered.set()
        assert resume.wait(20)
        return valid

    monkeypatch.setattr(form_type, 'is_valid', validate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: client.post(path, payload))
        try:
            assert entered.wait(20)
            exclude_report(patient, report)
        finally:
            resume.set()
        response = future.result(timeout=20)
    assert_discarded(response, patient, document, report)
