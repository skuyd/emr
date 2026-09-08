"""Real transactions and committed source/actor changes in an isolated PG DB."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from uuid import uuid4

from django.db import connection, transaction
import pytest

from apps.cloud_imaging import scan_services, views
from apps.cloud_imaging.models import CloudImagingScan
from apps.cloud_imaging.readmodels import document_snapshot, source_details
from apps.cloud_imaging.services import CloudConflict, add_manual_source, revise_source
from apps.patients.access import authorize_patient, change_membership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.cloud_imaging.factories import stored_document
from tests.cloud_imaging.test_scans import queued
from tests.cloud_imaging.test_source_services import FIRST_URL
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgres():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the dedicated PostgreSQL cloud-source test database')


def case(django_user_model, marker):
    owner, patient, member, actor, membership = family(django_user_model, 'pg-cloud-' + marker)
    document, page, store = stored_document(patient)
    return owner, patient, member, actor, membership, document, page, store


def manual(patient, actor, document, page):
    material = document_snapshot(patient, actor=actor, document_id=document.pk)
    return add_manual_source(patient, actor=actor, document_id=document.pk, page_id=page.pk,
        url=FIRST_URL, title='Synthetic cloud source', expected_source=material['input_token'], operation_id=uuid4())


def test_duplicate_scan_request_waits_and_reuses_one_original_actor_job(django_user_model):
    _, patient, _, actor, _, document, _, _ = case(django_user_model, 'one-job')
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = queued(patient, document, actor=actor)
            blocker = backend_pid()
            second = pool.submit(thread_call, lambda: queued(patient, document), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
            assert CloudImagingScan.objects.filter(document=document).count() == 1
        result = second.result(timeout=20)
    assert result.pk == first.pk and result.requested_by_id == actor.pk
    assert document.cloud_imaging_scans.count() == 1


def test_second_worker_cannot_acquire_or_publish_a_running_lease(django_user_model, monkeypatch):
    _, patient, _, actor, _, document, _, store = case(django_user_model, 'one-lease')
    scan = queued(patient, document, actor=actor)
    entered, resume = Event(), Event()
    calls = []
    real = scan_services.decode_page
    def paused(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(True)
        entered.set()
        assert resume.wait(20)
        return result
    monkeypatch.setattr(scan_services, 'decode_page', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(thread_call, lambda: scan_services.run_scan(scan.pk, store))
        try:
            assert entered.wait(20)
            scan_services.run_scan(scan.pk, store)
            assert len(calls) == 1 and not scan.evidence.exists()
        finally:
            resume.set()
        first.result(timeout=20)
    scan.refresh_from_db()
    assert scan.status == 'SUCCEEDED' and scan.attempts == 1 and scan.evidence.count() == 1


@pytest.mark.parametrize('change', ['revoke', 'downgrade', 'parse', 'trash'])
def test_worker_rejects_actual_committed_change_during_local_decode(django_user_model, monkeypatch, change):
    from apps.documents.lifecycle import move_to_trash
    from tests.facts.factories import parsed_facts

    _, patient, _, actor, membership, document, _, store = case(django_user_model, 'publish-' + change)
    scan = queued(patient, document, actor=actor)
    entered, resume = Event(), Event()
    real = scan_services.decode_page
    def paused(*args, **kwargs):
        result = real(*args, **kwargs)
        entered.set()
        assert resume.wait(20)
        return result
    monkeypatch.setattr(scan_services, 'decode_page', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: scan_services.run_scan(scan.pk, store))
        try:
            assert entered.wait(20)
            if change in {'revoke', 'downgrade'}:
                change_membership(patient, patient.account, membership.pk, expected_revision=0,
                    **({'revoke': True} if change == 'revoke' else {'role': 'VIEWER'}))
            elif change == 'parse':
                parsed_facts(patient, ['新发布的合成原件文字'], document=document)
            else:
                move_to_trash(patient, document.pk, actor=patient.account)
        finally:
            resume.set()
        future.result(timeout=20)
    scan.refresh_from_db()
    assert scan.status == 'INVALIDATED' and scan.summary['unknown_pages'] == 1
    assert not scan.evidence.exists() and not document.cloud_imaging_sources.exists()


def test_stale_decision_waits_for_first_revision_and_cannot_overwrite_it(django_user_model):
    _, patient, _, actor, _, document, page, _ = case(django_user_model, 'revision')
    source = manual(patient, actor, document, page)
    current = source_details(patient, actor=actor, source_id=source.pk)
    parameters = dict(actor=actor, source_id=source.pk, expected_revision=1,
        expected_source=current['source_token'], checked_original=True)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_source(patient, **parameters, action='CONFIRM', operation_id=uuid4())
            blocker = backend_pid()
            future = pool.submit(thread_call,
                lambda: revise_source(patient, **parameters, action='EXCLUDE', operation_id=uuid4()), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(CloudConflict):
            future.result(timeout=20)
    source.refresh_from_db()
    assert source.status == 'CONFIRMED' and source.revision_number == 2 and source.revisions.count() == 2


def test_author_foreign_keys_commit_while_account_deletion_waits_for_patient(django_user_model):
    from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
    from apps.accounts.models import AccountDeletionJob

    _, patient, _, actor, _, document, page, _ = case(django_user_model, 'author-fk')
    identity = actor.pk
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(patient, actor, 'write', lock=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: request_account_deletion(identity,
                document_dispatch=lambda _: None, account_dispatch=lambda _: None), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
            source = manual(patient, actor, document, page)
            scan = queued(patient, document, actor=actor)
        future.result(timeout=20)
    assert source.created_by_id == identity and scan.requested_by_id == identity
    job = AccountDeletionJob.objects.get(account_id=identity)
    assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
    source.refresh_from_db()
    scan.refresh_from_db()
    assert source.created_by_id is None and scan.requested_by_id is None
    assert source.revisions.get().author_id is None
    scan_services.recover_scans(dispatch=lambda _: pytest.fail('Revoked requester must not be dispatched'))
    scan.refresh_from_db()
    assert scan.status == 'INVALIDATED'


@pytest.mark.parametrize('page_kind', ['document', 'source'])
@pytest.mark.parametrize('change', ['revision', 'author_purge'])
def test_html_read_discards_content_after_real_revision_or_author_commit(django_user_model, monkeypatch, page_kind, change):
    from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
    from apps.accounts.models import AccountDeletionJob

    owner, patient, _, actor, _, document, page, _ = case(django_user_model, 'read-' + page_kind + change)
    source = manual(patient, actor, document, page)
    url = f'/records/{document.pk}/cloud-imaging/' if page_kind == 'document' else f'/cloud-imaging/{source.pk}/'
    entered, resume = Event(), Event()
    real = views._render
    def paused(*args, **kwargs):
        result = real(*args, **kwargs)
        entered.set()
        assert resume.wait(20)
        return result
    monkeypatch.setattr(views, '_render', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: owner.get(url))
        try:
            assert entered.wait(20)
            if change == 'revision':
                current = source_details(patient, actor=actor, source_id=source.pk)
                revise_source(patient, actor=actor, source_id=source.pk, action='CONFIRM', expected_revision=1,
                    expected_source=current['source_token'], checked_original=True, operation_id=uuid4())
            else:
                request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
                job = AccountDeletionJob.objects.get(account_id=actor.pk)
                assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
        finally:
            resume.set()
        response = future.result(timeout=20)
    assert response.status_code == 409
    assert b'SYNTHETIC_FIRST' not in response.content and str(actor.pk).encode() not in response.content
