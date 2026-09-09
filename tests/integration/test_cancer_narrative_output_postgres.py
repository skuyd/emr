"""Narrative consumers across distinct PostgreSQL connections and normal COMMIT."""
from concurrent.futures import ThreadPoolExecutor
import json
from queue import Queue

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
import pytest

from apps.accounts.deletion import purge_account_deletion, request_account_deletion
from apps.cancer_ordering.models import CandidateRevision, SelectionRevision
from apps.cancer_ordering.services import collect_current
from apps.exports import services, views
from apps.exports.errors import ExportInputError, ExportUnavailable
from apps.exports.models import ExportJob
from apps.facts.revisions import revise_fact
from apps.operations.models import AuditEvent
from apps.patients import share_views
from apps.patients.access import Capability, authorize_patient
from apps.patients.models import PatientShare
from apps.patients.sharing import create_share
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.cancer_ordering.narrative_consumer_factories import (
    CONTEXT, change, confirm_candidate, consumer_case, form_request, preview, ready, row, selected, share,
)
from tests.documents.fakes import InMemoryObjectStore
from tests.integration.test_cancer_ordering_postgres import _fresh_state
from tests.integration.test_cancer_output_postgres import during_boundary
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def note(request, name, value):
    request.node.user_properties.append((name, json.dumps(value, sort_keys=True)))


@pytest.fixture(autouse=True)
def require_postgresql(request, settings):
    if connection.vendor != 'postgresql':
        if settings.SETTINGS_MODULE == 'config.settings.postgres_test':
            pytest.fail('The required narrative consumer database must be PostgreSQL')
        pytest.skip('Requires the isolated narrative consumer PostgreSQL database')
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_database(), pg_backend_pid()')
        database, pid = cursor.fetchone()
    note(request, 'database', database)
    note(request, 'test_backend_pid', pid)
    assert not connection.in_atomic_block


def committed(request, label, action):
    original_pid, pids = backend_pid(), Queue()
    def run():
        assert not connection.in_atomic_block
        result = action()
        assert not connection.in_atomic_block
        return result
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, run, pids)
        other_pid = pids.get(timeout=10)
        assert other_pid != original_pid
        result = future.result(timeout=30)
    note(request, label, {'caller_pid': original_pid, 'other_pid': other_pid, 'completed_outside_atomic': True})
    return result


def at_boundary(request, monkeypatch, module, name, operation, mutation):
    def read():
        note(request, 'request_backend_pid', backend_pid())
        return operation()
    def write():
        assert not connection.in_atomic_block
        mutation()
        assert not connection.in_atomic_block
        note(request, 'mutation_commit', {'backend_pid': backend_pid(), 'outside_atomic': True})
    return during_boundary(monkeypatch, module, name, read, write)


def committed_job(request, job):
    return committed(request, 'job_readback', lambda: ExportJob.objects.values(
        'status', 'snapshot', 'options', 'cleanup_pending', 'object_key', 'filename', 'content_type', 'sha256', 'byte_size').get(pk=job.pk))


def hidden(data):
    assert data['status'] == 'INVALIDATED' and data['snapshot'] == {} and data['options'] == {} and data['cleanup_pending']


@pytest.mark.parametrize('page,method,mutation', [
    ('candidate', 'get', 'parent'), ('candidate', 'post', 'parent'),
    ('prepare', 'get', 'new_input'), ('prepare', 'post', 'history'),
    ('shares', 'get', 'membership'), ('shares', 'post', 'parent'),
])
@pytest.mark.parametrize('changed', [False, True])
def test_committed_change_discards_initial_form_material(request, django_user_model, monkeypatch, page, method, mutation, changed):
    case = consumer_case(django_user_model, 'c3a-pg-form-' + page + method + str(changed), history=mutation == 'history')
    initial, count = row(case), CandidateRevision.objects.count()
    module, operation = form_request(case, page, method)
    response = at_boundary(request, monkeypatch, module, 'render', operation,
                            lambda: change(case, mutation if changed else 'none'))
    expected = (403 if mutation == 'membership' else 409) if changed else 200 if method == 'get' else 400
    assert response.status_code == expected
    body = response.content.decode()
    if changed:
        assert CONTEXT not in body and str(case.candidate.pk) not in body
        assert initial['current_source_token'] not in body and 'name="expected_revision"' not in body
    else:
        assert '肺癌' in body and str(case.candidate.pk) in body
        if method == 'post':
            assert response.context['form'].errors
    result = committed(request, 'form_no_writes', lambda: (
        CandidateRevision.objects.count(), SelectionRevision.objects.count(), ExportJob.objects.count(), PatientShare.objects.count()))
    assert result == (count, 0, 0, 0)
    response.close()


@pytest.mark.parametrize('kind', ['preview', 'share'])
@pytest.mark.parametrize('changed', [False, True])
def test_waiting_creation_uses_original_selection_guard(request, django_user_model, kind, changed):
    case = consumer_case(django_user_model, 'c3a-pg-create-' + kind + str(changed))
    selection, pids = selected(case), Queue()
    operation = (lambda: services.create_preview(case.patient, case.client.session.session_key, selection, actor=case.actor)) if kind == 'preview' else (
        lambda: create_share(case.patient, case.actor, selection))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(case.patient, case.actor, Capability.WRITE, lock=True)
            if changed:
                change(case, 'parent' if kind == 'preview' else 'new_input')
            blocker = backend_pid()
            future = pool.submit(thread_call, operation, pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=waiter, blocker_pid=blocker)
            note(request, 'creation_wait', {'blocker': blocker, 'waiter': waiter})
        assert not connection.in_atomic_block
        if changed:
            with pytest.raises(ExportInputError):
                future.result(timeout=30)
        else:
            future.result(timeout=30)
    counts = committed(request, 'creation_readback', lambda: (ExportJob.objects.count(), PatientShare.objects.count()))
    assert counts == ((0, 0) if changed else (1, 0) if kind == 'preview' else (0, 1))


@pytest.mark.parametrize('page,method,mutation', [('preview', 'get', 'parent'), ('preview', 'post', 'history'), ('share', 'get', 'new_input')])
@pytest.mark.parametrize('changed', [False, True])
def test_rendered_output_is_discarded_and_scrub_is_committed(request, django_user_model, monkeypatch, page, method, mutation, changed):
    case = consumer_case(django_user_model, 'c3a-pg-html-' + page + method + str(changed), history=mutation == 'history')
    if page == 'preview':
        output = preview(case)
        client, path, module = case.client, f'/visit/{output.pk}/', views
    else:
        output, client, path = share(case, django_user_model, 'c3a-pg-html-reader')
        module = share_views
    operation = (lambda: client.get(path)) if method == 'get' else (
        lambda: client.post(path, {'format': 'BAD', 'patient_id': str(case.patient.pk)}))
    response = at_boundary(request, monkeypatch, module, 'render', operation,
        lambda: change(case, mutation if changed else 'none', job=output if page == 'preview' else None))
    assert response.status_code == ((409 if page == 'preview' else 410) if changed else 200 if method == 'get' else 400)
    if changed:
        assert '肺癌' not in response.content.decode()
    else:
        assert '肺癌' in response.content.decode()
    if page == 'preview':
        data = committed_job(request, output)
        if changed:
            hidden(data)
        else:
            assert data['status'] == 'PREVIEW' and data['snapshot']
    else:
        data = committed(request, 'share_readback', lambda: PatientShare.objects.values('snapshot', 'snapshot_digest', 'invalidated_at').get(pk=output.pk))
        if changed:
            assert data['snapshot'] == {} and data['snapshot_digest'] == '' and data['invalidated_at'] is not None
            assert client.get(path + 'status/').status_code == 410 and client.get(path).status_code == 410
            assert committed(request, 'single_invalidation_audit', lambda: AuditEvent.objects.filter(action='share_invalidated').count()) == 1
        else:
            assert data['snapshot'] and data['invalidated_at'] is None
    response.close()


@pytest.mark.parametrize('changed', [False, True])
def test_actual_artifact_cannot_be_published_after_parent_commit(request, django_user_model, monkeypatch, changed):
    case = consumer_case(django_user_model, 'c3a-pg-worker-' + str(changed))
    job = preview(case)
    services.request_generation(case.patient, case.client.session.session_key, job.pk, {'format': 'json'},
                                actor=case.actor, dispatch=lambda _: None)
    store, artifacts = InMemoryObjectStore(), []
    original = services.build_artifact
    def capture(*args, **kwargs):
        artifact = original(*args, **kwargs)
        artifacts.append(artifact)
        return artifact
    monkeypatch.setattr(services, 'build_artifact', capture)
    def mutation():
        assert not store.objects and not store.calls
        change(case, 'parent' if changed else 'none', job=job)
    at_boundary(request, monkeypatch, services, 'build_artifact', lambda: services.generate_export(job.pk, store), mutation)
    assert artifacts and all(item.stream.closed for item in artifacts)
    data = committed_job(request, job)
    if changed:
        hidden(data)
        assert not store.objects and not store.calls
        assert committed(request, 'unused_attempt_cleanup', lambda: services.cleanup_export(job.pk, store))
        assert not committed(request, 'unused_attempt_readback', lambda: job.attempts.filter(cleaned_at__isnull=True).exists())
        note(request, 'storage_scope', 'artifact was built but never stored; this is not deletion of a READY object')
    else:
        assert data['status'] == 'READY' and data['byte_size'] > 0 and data['object_key'] in store.objects


@pytest.mark.parametrize('kind,mutation,phase', [
    ('pdf', 'new_input', 'between_chunks'), ('download', 'history', 'between_chunks'),
    ('download', 'history', 'inside_read'), ('download', 'membership', 'between_chunks'),
])
@pytest.mark.parametrize('changed', [False, True])
def test_started_stream_and_real_ready_cleanup_follow_committed_changes(request, django_user_model, monkeypatch, kind, mutation, phase, changed):
    case = consumer_case(django_user_model, 'c3a-pg-stream-' + kind + mutation + phase + str(changed), history=mutation == 'history')
    store = InMemoryObjectStore()
    if kind == 'download':
        job, original = ready(case, store, kind='zip' if phase == 'inside_read' else 'json')
        note(request, 'ready_before_stream', original)
        monkeypatch.setattr(views, 'get_object_store', lambda: store)
    else:
        job, original = preview(case), None
    response = case.client.get(f'/visit/{job.pk}/{kind}/')
    assert response.status_code == 200
    response.block_size = 96
    chunks = iter(response.streaming_content)
    first = next(chunks)
    assert len(first) == 96
    note(request, 'already_delivered_bytes', len(first))
    def mutation_commit():
        return committed(request, 'stream_mutation', lambda: change(case, mutation if changed else 'none', job=job))
    if phase == 'inside_read':
        underlying = response._guarded_stream.stream
        class ReadBoundary:
            seen = False
            def read(self, size):
                payload = underlying.read(size)
                if not self.seen:
                    self.seen = True
                    assert payload
                    note(request, 'bytes_read_before_commit', len(payload))
                    mutation_commit()
                return payload
            def __getattr__(self, name):
                return getattr(underlying, name)
        response._guarded_stream.stream = ReadBoundary()
    else:
        mutation_commit()
    try:
        remaining = list(chunks)
        if changed:
            assert remaining == [] and response._guarded_stream.denied
            assert response._guarded_stream.stream.closed
            hidden(committed_job(request, job))
            if original:
                # Keep the original key and prove existence before deleting it.
                assert original['object_key'] in store.objects and original['byte_size'] > 0
                assert committed(request, 'ready_object_cleanup', lambda: services.cleanup_export(job.pk, store))
                data = committed_job(request, job)
                assert not data['cleanup_pending'] and data['byte_size'] == 0
                assert all(data[key] == '' for key in ('object_key', 'filename', 'content_type', 'sha256'))
                assert not committed(request, 'cleaned_attempt_readback', lambda: job.attempts.filter(cleaned_at__isnull=True).exists())
                assert original['object_key'] not in store.objects
                assert committed(request, 'idempotent_ready_cleanup', lambda: services.cleanup_export(job.pk, store))
                assert original['object_key'] not in store.objects
                note(request, 'previously_present_ready_object_deleted', True)
        else:
            assert remaining and not response._guarded_stream.denied and response._guarded_stream.exhausted
            data = committed_job(request, job)
            assert data['status'] == ('READY' if original else 'PREVIEW') and data['snapshot']
    finally:
        response.close()
    assert response._guarded_stream.finished and response._guarded_stream.stream.closed


@pytest.mark.parametrize('operation', ['create', 'access'])
@pytest.mark.parametrize('purge', [False, True])
def test_output_patient_guard_allows_parent_confirmation_and_author_fk_cleanup(request, django_user_model, operation, purge):
    case = consumer_case(django_user_model, 'c3a-pg-fk-' + operation + str(purge), history=True)
    history_id, pids = case.historical.pk, Queue()
    def waiter_action():
        if purge:
            deletion = request_account_deletion(history_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
            assert purge_account_deletion(deletion.pk).outcome == 'PURGED'
        else:
            with transaction.atomic():
                authorize_patient(case.patient, case.actor, Capability.WRITE, lock=True)
        assert not connection.in_atomic_block
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(case.patient, case.actor, Capability.WRITE, lock=True)
            case.parent.refresh_from_db()
            revise_fact(case.patient, case.parent.pk, actor=case.reviewer, action='CONFIRM',
                expected_revision=case.parent.revision_number, checked_original=True)
            collect_current(case.patient, actor=case.patient.account)
            confirm_candidate(case)
            if operation == 'access':
                job = preview(case)
            blocker = backend_pid()
            future = pool.submit(thread_call, waiter_action, pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=waiter, blocker_pid=blocker)
            note(request, 'author_fk_wait', {'blocker': blocker, 'waiter': waiter, 'purge': purge})
            if operation == 'create':
                job = preview(case)
            else:
                assert services.get_preview(case.patient, case.client.session.session_key, job.pk, actor=case.actor).pk == job.pk
        future.result(timeout=30)
    assert not connection.in_atomic_block
    data = committed_job(request, job)
    assert data['status'] == 'PREVIEW'  # Author cleanup did not eagerly invalidate this issuer's job.
    if purge:
        assert committed(request, 'author_cleanup_readback', lambda: (
            type(case.historical).objects.filter(pk=history_id).exists(),
            case.parent.revisions.order_by('sequence').first().author_id,
            case.parent.revisions.order_by('-sequence').first().author_id)) == (False, None, case.reviewer.pk)
        with pytest.raises(ExportUnavailable):
            services.get_preview(case.patient, case.client.session.session_key, job.pk, actor=case.actor)
        hidden(committed_job(request, job))
    else:
        assert services.get_preview(case.patient, case.client.session.session_key, job.pk, actor=case.actor).pk == job.pk
