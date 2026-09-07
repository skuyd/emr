from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, transaction

from apps.patients.access import authorize_patient, change_membership
from apps.patients.profile import save_product_feedback
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def thread_call(action, pid_queue=None):
    close_old_connections()
    try:
        if pid_queue is not None:
            pid_queue.put(backend_pid())
        return action()
    finally:
        connection.close()


def state(pid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = %s", [pid])
        return cursor.fetchone()


@pytest.mark.parametrize("account_delete", [False, True])
def test_membership_revocation_serializes_with_actor_foreign_key_writes(django_user_model, account_delete):
    from apps.accounts.deletion import request_account_deletion

    _, patient, _, actor, membership = family(django_user_model, "pg-family-delete" if account_delete else "pg-family-revoke")
    def revoke():
        if account_delete:
            return request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        return change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(patient, actor, "write", lock=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, revoke, pid_queue)
            waiter = pid_queue.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
            # The author FK must commit while account deletion holds its account
            # mutex. FOR UPDATE on Account here would deadlock at FK validation.
            feedback = save_product_feedback(patient, "synthetic contribution", actor=actor)
        future.result(timeout=15)
    assert feedback.created_by_id == actor.pk
    with pytest.raises(PermissionDenied):
        save_product_feedback(patient, "after revocation", actor=actor)


def test_member_revocation_during_export_build_prevents_storage_publication(django_user_model, monkeypatch):
    from apps.exports import services
    from tests.documents.fakes import InMemoryObjectStore
    from tests.documents.test_detail_viewer import _document

    _, patient, client, actor, membership = family(django_user_model, "pg-family-export")
    _document(patient)
    job = services.create_preview(patient, client.session.session_key, {"mode": "all"}, actor=actor)
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, actor=actor, dispatch=lambda _: None)
    entered, release = Event(), Event()
    build = services.build_artifact
    def paused_build(*args, **kwargs):
        artifact = build(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=15)
        return artifact
    monkeypatch.setattr(services, "build_artifact", paused_build)
    store = InMemoryObjectStore()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: services.generate_export(job.pk, store))
        try:
            assert entered.wait(timeout=10)
            change_membership(patient, patient.account, membership.pk, role="VIEWER", expected_revision=0)
        finally:
            release.set()
        future.result(timeout=15)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {} and store.objects == {}


def test_patient_deletion_fences_a_running_worker_before_publication(django_user_model):
    from apps.patients.deletion import request_patient_deletion
    from apps.processing.runner import run_processing, PipelineResult, ExecutionState
    from tests.processing.test_runner import make_run

    _, document, run = make_run(django_user_model)
    entered, release = Event(), Event()
    def pipeline(context):
        entered.set()
        assert release.wait(timeout=15)
        with transaction.atomic():
            context.assert_current()
        return PipelineResult.organized()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: run_processing(run.pk, pipeline))
        try:
            assert entered.wait(timeout=10)
            request_patient_deletion(document.patient_id, document.patient.account, document_dispatch=lambda _: None)
        finally:
            release.set()
        assert future.result(timeout=15).state == ExecutionState.LEASE_LOST
    run.refresh_from_db()
    document.refresh_from_db()
    assert not run.is_current and document.deleted_at is not None


def test_onboarding_waits_for_account_deletion_and_cannot_recreate_data(django_user_model):
    from apps.accounts.deletion import request_account_deletion
    from apps.patients.models import Patient
    from apps.patients.services import create_patient_space
    from tests.documents.test_detail_viewer import _patient, CONFIRMATIONS, EVIDENCE

    _, patient = _patient(django_user_model, "pg-family-onboarding")
    actor = patient.account
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: create_patient_space(actor, "stale creation", CONFIRMATIONS, EVIDENCE), pid_queue)
            waiter = pid_queue.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(PermissionDenied):
            future.result(timeout=15)
    assert not Patient.objects.filter(account=actor, deleted_at__isnull=True).exists()
