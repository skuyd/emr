"""Real lock waits, revocation and source updates in an isolated PostgreSQL DB."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.facts.revisions import revise_fact
from apps.patients.access import authorize_patient, change_membership
from apps.treatments.derivations import decide_proposal, persist_proposals, proposal_preview
from apps.treatments.models import TreatmentCycle, TreatmentDerivationRun, TreatmentRevision
from apps.treatments.services import TreatmentConflict
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family
from tests.treatments.test_derivation_service import fact

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def test_identical_cycle_decision_really_waits_and_commits_once(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, "pg-treat-replay")
    fact(patient)
    preview = proposal_preview(patient, actor=actor)
    proposal = preview["proposals"]["cycles"][0]
    arguments = dict(actor=actor, expected_fingerprint=preview["input_fingerprint"], action="CONFIRM",
                     expected_revision=0, checked_original=True, operation_id=uuid.uuid4())
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = decide_proposal(patient, proposal["id"], **arguments)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: decide_proposal(patient, proposal["id"], **arguments), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        second = future.result(timeout=15)
    assert first.pk == second.pk
    assert TreatmentRevision.objects.filter(cycle=first.cycle).count() == 1
    assert TreatmentDerivationRun.objects.count() == 1


def test_source_revision_fences_proposal_waiting_on_the_patient_guard(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, "pg-treat-source")
    original = fact(patient)
    preview = proposal_preview(patient, actor=actor)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_fact(patient, original.pk, actor=patient.account, action="CORRECT", expected_revision=0,
                        changes={"text": "2024-04-01给予方案乙化疗。"}, checked_original=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: persist_proposals(patient, actor=actor,
                expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4()), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(TreatmentConflict):
            future.result(timeout=15)
    assert not TreatmentDerivationRun.objects.exists() and not TreatmentCycle.objects.exists()


def test_revoked_editor_cannot_save_a_waiting_derivation(django_user_model):
    _, patient, _, actor, membership = family(django_user_model, "pg-treat-revoke")
    fact(patient)
    preview = proposal_preview(patient, actor=actor)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: persist_proposals(patient, actor=actor,
                expected_fingerprint=preview["input_fingerprint"], operation_id=uuid.uuid4()), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(PermissionDenied):
            future.result(timeout=15)
    assert not TreatmentDerivationRun.objects.exists()


@pytest.mark.parametrize("account_delete", [False, True])
def test_pending_revocation_and_account_deletion_do_not_deadlock_real_author_foreign_keys(django_user_model, account_delete):
    from apps.accounts.deletion import request_account_deletion
    _, patient, _, actor, membership = family(django_user_model, "pg-treat-author-delete" if account_delete else "pg-treat-author-revoke")
    fact(patient)
    preview = proposal_preview(patient, actor=actor)
    proposal = preview["proposals"]["cycles"][0]
    def revoke():
        if account_delete:
            return request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        return change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(patient, actor, "write", lock=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, revoke, pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
            decision = decide_proposal(patient, proposal["id"], actor=actor, expected_fingerprint=preview["input_fingerprint"],
                action="CONFIRM", expected_revision=0, checked_original=True, operation_id=uuid.uuid4())
        future.result(timeout=15)
    assert decision.author_id == actor.pk and decision.cycle.created_by_id == actor.pk
    assert TreatmentDerivationRun.objects.get().requested_by_id == actor.pk
    with pytest.raises(PermissionDenied):
        proposal_preview(patient, actor=actor)


def test_read_preview_revalidates_actor_after_consistent_material_build(django_user_model, monkeypatch):
    from apps.treatments import derivations
    _, patient, _, actor, membership = family(django_user_model, "pg-treat-read-revoke")
    fact(patient)
    entered, release = Event(), Event()
    real = derivations.authorize_patient
    def paused(*args, **kwargs):
        if not kwargs.get("lock"):
            entered.set()
            assert release.wait(timeout=15)
        return real(*args, **kwargs)
    monkeypatch.setattr(derivations, "authorize_patient", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: derivations.proposal_preview(patient, actor=actor))
        try:
            assert entered.wait(timeout=10)
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        finally:
            release.set()
        with pytest.raises(PermissionDenied):
            future.result(timeout=15)
