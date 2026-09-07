"""Actual PostgreSQL wait relationships for material decisions and revocation."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.accounts.deletion import request_account_deletion
from apps.documents.models import ProcessingRun
from apps.patients.access import authorize_patient, change_membership
from apps.processing.models import MaterialDecision
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_material_family import keep, material_family
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def test_simultaneous_same_material_decision_waits_and_queues_once(django_user_model):
    _, document, version, _, actor, _ = material_family(django_user_model, "pg-material-duplicate")
    queued = []
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = keep(document, version, actor, queued.append)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: keep(document, version, actor, queued.append), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        repeat = future.result(timeout=15)
    assert first.pk == repeat.pk
    assert queued == [first.processing_run_id]
    assert MaterialDecision.objects.filter(document=document).count() == 1
    assert ProcessingRun.objects.filter(document=document).count() == 2


@pytest.mark.parametrize("account_delete", [False, True])
def test_actor_fk_decision_can_commit_while_revocation_waits_and_retry_is_fenced(django_user_model, account_delete):
    _, document, version, _, actor, membership = material_family(django_user_model, "pg-material-revoke")

    def revoke():
        if account_delete:
            return request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        return change_membership(document.patient, document.patient.account, membership.pk, revoke=True, expected_revision=0)

    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(document.patient, actor, "write", lock=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, revoke, pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
            # The new decision author FK and retry requested_by FK must commit
            # even when deletion holds Account NO KEY UPDATE and awaits Patient.
            decision = keep(document, version, actor)
        future.result(timeout=15)
    assert decision.author_id == actor.pk
    run = ProcessingRun.objects.get(pk=decision.processing_run_id)
    assert run.requested_by_id == actor.pk and run.error_code == "access_revoked"
    assert not run.is_current
    with pytest.raises(PermissionDenied):
        keep(document, version, actor)
