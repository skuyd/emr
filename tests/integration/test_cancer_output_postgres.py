"""Actual committed changes at form, preview, publication and stream boundaries."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

from django.db import connection
import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import select_ordering
from apps.exports import services, views
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.operations.models import AuditEvent
from apps.patients import share_views
from apps.patients.access import change_membership
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_services import _collect, _row, _select
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call
from tests.patients.test_family_access import family
from tests.patients.test_family_shares import exchange


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated cancer output PostgreSQL database')


def fixture(django_user_model, name):
    owner, patient, member, actor, membership = family(django_user_model, name, role='ADMIN')
    _, version = _collect(patient)
    state = resolve_ordering(patient)
    select_ordering(patient, actor=actor, mode='MANUAL_PROFILE', profile='LUNG',
                    expected_revision=state['revision_number'], expected_fingerprint=state['fingerprint'])
    assert owner.get('/records/').status_code == 200
    chosen = selected_body(patient, cancer_candidate_ids=[_row(patient)['id']], include_indicator_ordering=True)
    return owner, patient, member, actor, membership, version, chosen


def mutate(patient, actor, version, kind):
    assert not connection.in_atomic_block
    if kind == 'new_input':
        parsed_facts(patient, ['出院诊断：胰腺癌。'])
    elif kind == 'parent':
        fact = Fact.objects.get(parsing_version=version)
        revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
    elif kind == 'selection':
        _select(patient, 'GENERAL')
    else:
        deletion = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        assert purge_account_deletion(deletion.pk).outcome == AccountDeletionOutcome.PURGED
        assert not type(actor).objects.filter(pk=actor.pk).exists()
    assert not connection.in_atomic_block


def during_boundary(monkeypatch, module, name, operation, change):
    original = getattr(module, name)
    entered, release, pids = Event(), Event(), Queue()
    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        if not entered.is_set():
            assert not connection.in_atomic_block
            entered.set()
            assert release.wait(30)
        return result
    monkeypatch.setattr(module, name, pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, operation, pids)
        request_pid = pids.get(timeout=10)
        try:
            assert request_pid != backend_pid() and entered.wait(25)
            change()
        finally:
            release.set()
        return future.result(timeout=30)


@pytest.mark.parametrize('kind', ['export', 'share'])
@pytest.mark.parametrize('change', ['new_input', 'membership'])
def test_form_render_rechecks_actual_committed_source_and_current_capability(django_user_model, monkeypatch, kind, change):
    _, patient, member, actor, membership, version, _ = fixture(django_user_model, 'cancer-pg-form-' + kind + change)
    path = '/visit/' if kind == 'export' else f'/patients/{patient.pk}/shares/'
    change_fn = (lambda: change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)) if change == 'membership' else (
        lambda: mutate(patient, actor, version, change))
    response = during_boundary(monkeypatch, views if kind == 'export' else share_views, 'render',
                               lambda: member.get(path), change_fn)
    assert response.status_code == (403 if change == 'membership' else 409)
    assert '肺癌' not in response.content.decode() and _row(patient)['id'] not in response.content.decode()
    response.close()


@pytest.mark.parametrize('kind', ['preview', 'share'])
@pytest.mark.parametrize('change', ['new_input', 'parent', 'author_purge'])
def test_existing_selected_html_discards_body_and_commits_snapshot_scrub(django_user_model, monkeypatch, kind, change):
    owner, patient, _, actor, _, version, chosen = fixture(django_user_model, 'cancer-pg-html-' + kind + change)
    if kind == 'preview':
        output = services.create_preview(patient, owner.session.session_key, chosen, actor=patient.account)
        client, path, module = owner, f'/visit/{output.pk}/', views
    else:
        created = create_share(patient, patient.account, chosen)
        output = created.share
        client, _ = _patient(django_user_model, 'cancer-pg-share-viewer')
        path, module = f'/shared/{exchange(client, created.token)}/', share_views
    initial = resolve_ordering(patient)['fingerprint']
    response = during_boundary(monkeypatch, module, 'render', lambda: client.get(path),
                               lambda: mutate(patient, actor, version, change))
    assert resolve_ordering(patient)['fingerprint'] != initial
    assert response.status_code == (409 if kind == 'preview' else 410)
    assert '肺癌' not in response.content.decode()
    output.refresh_from_db()
    assert output.snapshot == {}
    if kind == 'preview':
        assert output.status == 'INVALIDATED' and output.cleanup_pending
    else:
        assert output.invalidated_at is not None
        count = AuditEvent.objects.filter(action='share_invalidated').count()
        assert count == 1 and client.get(path).status_code == 410
        assert AuditEvent.objects.filter(action='share_invalidated').count() == count
    response.close()


@pytest.mark.parametrize('change', ['new_input', 'selection'])
def test_worker_cannot_publish_after_actual_artifact_and_committed_dependency_change(django_user_model, monkeypatch, change):
    owner, patient, _, actor, _, version, chosen = fixture(django_user_model, 'cancer-pg-publish-' + change)
    job = services.create_preview(patient, owner.session.session_key, chosen, actor=patient.account)
    services.request_generation(patient, owner.session.session_key, job.pk, {'format': 'json'},
                                actor=patient.account, dispatch=lambda _: None)
    store = InMemoryObjectStore()
    during_boundary(monkeypatch, services, 'build_artifact', lambda: services.generate_export(job.pk, store),
                    lambda: mutate(patient, actor, version, change))
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and job.cleanup_pending
    assert services.cleanup_export(job.pk, store) and not store.objects


@pytest.mark.parametrize('kind', ['pdf', 'download'])
@pytest.mark.parametrize('change', ['new_input', 'author_purge'])
def test_actual_later_output_chunk_stops_after_other_connection_commits(django_user_model, monkeypatch, kind, change):
    owner, patient, _, actor, _, version, chosen = fixture(django_user_model, 'cancer-pg-stream-' + kind + change)
    job = services.create_preview(patient, owner.session.session_key, chosen, actor=patient.account)
    store = InMemoryObjectStore()
    if kind == 'download':
        services.request_generation(patient, owner.session.session_key, job.pk, {'format': 'json'},
                                    actor=patient.account, dispatch=lambda _: None)
        services.generate_export(job.pk, store)
        monkeypatch.setattr(views, 'get_object_store', lambda: store)
    response = owner.get(f'/visit/{job.pk}/{kind}/')
    assert response.status_code == 200
    response.block_size = 96
    chunks = iter(response.streaming_content)
    assert len(next(chunks)) == 96
    pids = Queue()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(thread_call, lambda: mutate(patient, actor, version, change), pids)
            assert pids.get(timeout=10) != backend_pid()
            future.result(timeout=30)
        assert list(chunks) == [] and response._guarded_stream.denied
        job.refresh_from_db()
        assert job.status == 'INVALIDATED' and job.snapshot == {} and job.cleanup_pending
    finally:
        response.close()
    assert response._guarded_stream.finished
