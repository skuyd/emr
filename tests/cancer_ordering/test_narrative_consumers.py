"""Consumer contracts using real narrative parents; no concurrent-commit claims."""
from collections import Counter
from copy import deepcopy

import pytest

from apps.cancer_ordering.models import CandidateRevision, CollectionRun, SelectionRevision
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.exports import services, views
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportUnavailable, SnapshotChanged
from apps.exports.models import ExportJob
from apps.facts.revisions import revise_fact
from apps.labs.models import LabObservation
from apps.operations.models import AuditEvent
from apps.patients import share_views
from apps.patients.models import PatientShare
from tests.cancer_ordering.narrative_consumer_factories import (
    CONTEXT, assert_hidden, change, confirm_candidate, consumer_case, form_request, preview, row, selected, share,
)
from tests.cancer_ordering.test_lab_ordering import labs
from tests.cancer_ordering.test_narrative_views import uploaded_narrative
from tests.cancer_ordering.test_services import _select
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db(transaction=True)


def after_result(monkeypatch, module, name, operation, mutation):
    original = getattr(module, name)
    called = False
    results = []
    def after(*args, **kwargs):
        nonlocal called
        result = original(*args, **kwargs)
        results.append(result)
        if not called:
            called = True
            mutation()
        return result
    monkeypatch.setattr(module, name, after)
    result = operation()
    assert called
    return result, results


@pytest.mark.parametrize('page,method,mutation', [
    ('candidate', 'get', 'parent'), ('candidate', 'post', 'parent'),
    ('prepare', 'get', 'new_input'), ('prepare', 'post', 'history'),
    ('shares', 'get', 'membership'), ('shares', 'post', 'parent'),
])
@pytest.mark.parametrize('changed', [False, True])
def test_forms_discard_old_narrative_material_after_render(django_user_model, monkeypatch, page, method, mutation, changed):
    case = consumer_case(django_user_model, 'narrative-forms-' + page + method + str(changed), history=mutation == 'history')
    initial = row(case)
    count = CandidateRevision.objects.count()
    module, operation = form_request(case, page, method)
    response, _ = after_result(monkeypatch, module, 'render', operation,
                               lambda: change(case, mutation if changed else 'none'))
    expected = (403 if mutation == 'membership' else 409) if changed else (200 if method == 'get' else 400)
    assert response.status_code == expected
    body = response.content.decode()
    if changed:
        assert CONTEXT not in body and str(case.candidate.pk) not in body
        assert initial['current_source_token'] not in body and 'name="expected_revision"' not in body
    else:
        assert str(case.candidate.pk) in body and '肺癌' in body
        if method == 'post':
            assert response.context['form'].errors
    assert CandidateRevision.objects.count() == count and not SelectionRevision.objects.exists()
    assert not ExportJob.objects.exists() and not PatientShare.objects.exists()
    response.close()


@pytest.mark.parametrize('kind', ['export', 'share'])
@pytest.mark.parametrize('changed', [False, True])
def test_stale_post_does_not_replace_the_original_form_fingerprint(django_user_model, kind, changed):
    case = consumer_case(django_user_model, 'narrative-stale-post-' + kind + str(changed))
    initial = resolve_ordering(case.patient)['fingerprint']
    data = {'mode': 'documents', 'nickname': case.patient.display_name, 'sections': ['cancer_ordering'],
            'cancer_candidate_ids': [str(case.candidate.pk)], 'cancer_expected_fingerprint': initial,
            'patient_id': str(case.patient.pk), 'action': 'preview'}
    # A new uncollected input changes the full guard while the selected candidate stays available.
    change(case, 'new_input' if changed else 'none')
    path = '/visit/' if kind == 'export' else f'/patients/{case.patient.pk}/shares/'
    response = case.client.post(path, data)
    if changed:
        assert response.status_code == 400
        assert response.context['form']['cancer_expected_fingerprint'].value() == initial
        assert 'cancer_expected_fingerprint' in response.context['form'].errors
        assert 'cancer_candidate_ids' not in response.context['form'].errors
        assert not ExportJob.objects.exists() and not PatientShare.objects.exists()
    else:
        assert response.status_code == (302 if kind == 'export' else 201)
        assert (ExportJob.objects.count(), PatientShare.objects.count()) == ((1, 0) if kind == 'export' else (0, 1))
    response.close()


@pytest.mark.parametrize('method,mutation', [('get', 'parent'), ('post', 'history')])
@pytest.mark.parametrize('changed', [False, True])
def test_preview_html_rechecks_unselected_excerpt_dependencies(django_user_model, monkeypatch, method, mutation, changed):
    case = consumer_case(django_user_model, 'narrative-preview-' + method + str(changed), history=mutation == 'history')
    job = preview(case)
    path = f'/visit/{job.pk}/'
    operation = (lambda: case.client.get(path)) if method == 'get' else (
        lambda: case.client.post(path, {'format': 'BAD', 'patient_id': str(case.patient.pk)}))
    response, _ = after_result(monkeypatch, views, 'render', operation,
        lambda: change(case, mutation if changed else 'none', job=job))
    assert response.status_code == (409 if changed else 200 if method == 'get' else 400)
    if changed:
        assert '肺癌' not in response.content.decode()
        assert_hidden(job)
    else:
        assert '肺癌' in response.content.decode()
        job.refresh_from_db()
        assert job.status == 'PREVIEW' and job.snapshot
    response.close()


@pytest.mark.parametrize('changed', [False, True])
def test_shared_html_rechecks_new_uncollected_input_and_scrubs_once(django_user_model, monkeypatch, changed):
    case = consumer_case(django_user_model, 'narrative-share-html-' + str(changed))
    output, reader, path = share(case, django_user_model, 'narrative-share-reader')
    response, _ = after_result(monkeypatch, share_views, 'render', lambda: reader.get(path),
                               lambda: change(case, 'new_input' if changed else 'none'))
    assert response.status_code == (410 if changed else 200)
    output.refresh_from_db()
    if changed:
        assert '肺癌' not in response.content.decode() and output.snapshot == {} and output.snapshot_digest == ''
        assert output.invalidated_at is not None
        assert reader.get(path + 'status/').status_code == 410
        assert reader.get(path).status_code == 410
        assert AuditEvent.objects.filter(action='share_invalidated').count() == 1
    else:
        assert '肺癌' in response.content.decode() and output.snapshot and output.invalidated_at is None
    response.close()


@pytest.mark.parametrize('changed', [False, True])
def test_worker_rechecks_after_actual_artifact_before_any_storage_publication(django_user_model, monkeypatch, changed):
    case = consumer_case(django_user_model, 'narrative-worker-' + str(changed))
    job = preview(case)
    services.request_generation(case.patient, case.client.session.session_key, job.pk, {'format': 'json'},
                                actor=case.actor, dispatch=lambda _: None)
    store = InMemoryObjectStore()
    def mutation():
        assert store.objects == {} and not store.calls
        change(case, 'parent' if changed else 'none', job=job)
    _, artifacts = after_result(monkeypatch, services, 'build_artifact',
        lambda: services.generate_export(job.pk, store), mutation)
    assert artifacts and all(artifact.stream.closed for artifact in artifacts)
    job.refresh_from_db()
    if changed:
        assert_hidden(job)
        assert not store.calls and not store.objects  # Never stored, not a deletion proof.
        assert services.cleanup_export(job.pk, store)
        assert not job.attempts.filter(cleaned_at__isnull=True).exists()
    else:
        assert job.status == 'READY' and job.byte_size > 0 and job.object_key in store.objects


def test_rechecking_current_parent_creates_new_outputs_without_reviving_old_ones(django_user_model):
    case = consumer_case(django_user_model, 'narrative-recovery')
    confirm_candidate(case)
    original = deepcopy(case.candidate.original_data)
    prior = list(case.candidate.revisions.values())
    job = preview(case)
    output, reader, path = share(case, django_user_model, 'narrative-recovery-reader')
    change(case, 'parent', job=job)
    with pytest.raises(ExportUnavailable):
        services.get_preview(case.patient, case.client.session.session_key, job.pk, actor=case.actor)
    assert reader.get(path).status_code == 410
    case.parent.refresh_from_db()
    revise_fact(case.patient, case.parent.pk, actor=case.reviewer, action='CONFIRM',
                expected_revision=case.parent.revision_number, checked_original=True)
    collect_current(case.patient, actor=case.patient.account)
    assert row(case)['status'] != 'CONFIRMED' and row(case)['source_changed']
    confirm_candidate(case)
    case.candidate.refresh_from_db()
    assert case.candidate.original_data == original
    assert list(case.candidate.revisions.order_by('sequence').values())[:len(prior)] == prior
    with pytest.raises(ExportUnavailable):
        services.get_preview(case.patient, case.client.session.session_key, job.pk, actor=case.actor)
    assert reader.get(path).status_code == 410
    fresh = preview(case)
    assert services.get_preview(case.patient, case.client.session.session_key, fresh.pk, actor=case.actor).pk == fresh.pk
    fresh_share, fresh_reader, fresh_path = share(case, django_user_model, 'narrative-recovery-new-reader')
    assert fresh_reader.get(fresh_path).status_code == 200 and fresh_share.pk != output.pk


def test_uploaded_no_fact_source_reaches_preview_and_late_input_changes_only_the_ordering_dependency(uploaded_narrative):
    client, patient, document, candidate = uploaded_narrative()
    from tests.cancer_ordering.test_export_selection import selected_body
    selection = selected_body(patient, cancer_candidate_ids=[str(candidate.pk)])
    job = services.create_preview(patient, client.session.session_key, selection, actor=patient.account)
    assert job.source_bindings.filter(document=document).exists()
    new_document, _ = parsed_facts(patient, ['主诉：胰腺癌。'], document_type='UNKNOWN')
    assert not new_document.facts.exists() and not job.source_bindings.filter(document=new_document).exists()
    assert not CollectionRun.objects.filter(document=new_document).exists()
    job.refresh_from_db()
    assert job.status == 'PREVIEW'
    with pytest.raises(ExportUnavailable):
        services.get_preview(patient, client.session.session_key, job.pk, actor=patient.account)
    assert_hidden(job)


@pytest.mark.parametrize('change_other_patient', [False, True])
def test_unrelated_output_dependency_remains_usable(django_user_model, change_other_patient):
    case = consumer_case(django_user_model, 'narrative-unrelated-' + str(change_other_patient))
    if change_other_patient:
        snapshot = build_snapshot(case.patient, selected(case))
        _, other = _patient(django_user_model, 'narrative-unrelated-other')
        parsed_facts(other, ['主诉：胰腺癌。'], document_type='UNKNOWN')
    else:
        document, _ = parsed_facts(case.patient, ['一般资料。'], document_type='UNKNOWN')
        snapshot = build_snapshot(case.patient, {'mode': 'documents', 'document_ids': [str(document.pk)], 'sections': ['sources']})
        assert snapshot['cancer_ordering_fingerprint'] is None
        assert snapshot['cancer_candidates'] == snapshot['indicator_ordering'] == []
        change(case, 'new_input')
    assert_snapshot_current(case.patient, snapshot)


def test_labs_only_preserve_values_and_private_narrative_ordering_dependency(django_user_model):
    case = consumer_case(django_user_model, 'narrative-labs-only')
    observations = labs(case.patient)
    selection = {'mode': 'documents', 'document_ids': [str(item.parsing_version.document_id) for item in observations], 'sections': ['labs']}
    _select(case.patient, 'GENERAL')
    before = build_snapshot(case.patient, selection)
    _select(case.patient, 'AUTO')
    collect_current(case.patient, actor=case.patient.account)
    snapshot = build_snapshot(case.patient, selection)
    stored = list(LabObservation.objects.order_by('pk').values())
    assert snapshot['labs'][0]['standard_code'] == 'LAB_CEA'
    assert {item['id']: item for item in snapshot['labs']} == {item['id']: item for item in before['labs']}
    assert Counter(snapshot['card']['lab_ids']) == Counter(before['card']['lab_ids'])
    assert snapshot['cancer_candidates'] == snapshot['indicator_ordering'] == [] and snapshot['cancer_ordering_fingerprint']
    change(case, 'new_input')
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(case.patient, snapshot)
    assert list(LabObservation.objects.order_by('pk').values()) == stored
