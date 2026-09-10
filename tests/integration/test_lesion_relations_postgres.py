"""Independent PostgreSQL connections exercise the actual relation write locks."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from apps.facts.clinical_readmodels import report_state
from apps.facts.clinical_services import revise_report
from apps.facts.models import ClinicalReport, Fact
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.lesions.models import Lesion, LesionMatchProposal, LesionOperation
from apps.lesions.readmodels import review_observations
from apps.lesions.services import decide_proposal, generate_proposals, split_observations, undo_operation
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.lesions.factories import imaging_observation
from tests.lesions.test_relationships import expectations
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def prepared(django_user_model, name):
    _, patient, _, actor, membership = family(django_user_model, name)
    imaging_observation(django_user_model, patient=patient)
    imaging_observation(django_user_model, patient=patient, day="2026-09-01", size="15")
    generate_proposals(patient, actor=actor)
    proposal = LesionMatchProposal.objects.get(patient=patient)
    rows = review_observations(patient, actor=actor)
    arguments = {"proposal_id": proposal.pk, "expected_revision": proposal.revision_number,
                 "expected_fingerprint": proposal.fingerprint, "expectations": expectations(rows),
                 "checked_original": True, "name": "观察 A", "action": "CONFIRM"}
    return patient, actor, membership, proposal, rows, arguments


@pytest.mark.parametrize("change", ["parent", "field", "membership"])
def test_waiting_proposal_confirmation_rechecks_committed_source_or_writer(django_user_model, change):
    from apps.patients.access import change_membership

    patient, actor, membership, proposal, rows, arguments = prepared(django_user_model, "pg-lesion-" + change)
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == "parent":
                report = ClinicalReport.objects.get(pk=rows[0]["report_id"])
                revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE",
                               expected_revision=report.revision_number, expected_source=report_state(report)["current_source_token"])
            elif change == "field":
                field = Fact.objects.get(pk=next(field["id"] for field in rows[0]["fields"] if field["field_key"] == "lesion.site"))
                revise_fact(patient, field.pk, actor=patient.account, action="REVOKE", expected_revision=field.revision_number,
                            expected_source=effective_fact(field)["current_source_token"])
            else:
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: decide_proposal(patient, actor=actor, **arguments), pid_queue)
            waiter = pid_queue.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(PermissionDenied if change == "membership" else ValidationError):
            future.result(timeout=15)
    assert not Lesion.objects.filter(patient=patient).exists()
    assert proposal.revisions.count() == 1 and not LesionOperation.objects.filter(patient=patient, action="MATCH").exists()


def test_competing_confirmation_commits_one_complete_operation_and_rejects_old_preview(django_user_model):
    patient, actor, _, proposal, _, arguments = prepared(django_user_model, "pg-lesion-competing")
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            operation = decide_proposal(patient, actor=actor, **arguments)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: decide_proposal(patient, actor=actor, **arguments), pid_queue)
            waiter = pid_queue.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(ValidationError):
            future.result(timeout=15)
    assert Lesion.objects.filter(patient=patient).count() == 1
    assert operation.observation_revisions.count() == 2 and operation.proposal_revisions.count() == 1
    assert proposal.revisions.count() == 2 and all(row["usable"] for row in review_observations(patient, actor=actor))


def test_waiting_undo_cannot_restore_confirmation_after_field_revoke_and_restore(django_user_model):
    patient, actor, _, _, _, arguments = prepared(django_user_model, "pg-lesion-undo-source")
    decide_proposal(patient, actor=actor, **arguments)
    rows = review_observations(patient, actor=actor)
    lesion = Lesion.objects.get(patient=patient)
    selected = rows[1]
    operation = split_observations(patient, actor=actor, lesion_id=lesion.pk, expected_revision=lesion.revision_number,
        observation_ids=[selected["id"]], expectations=expectations([selected]), name="观察 B", checked_original=True)
    field = Fact.objects.get(pk=next(field["id"] for field in selected["fields"] if field["field_key"] == "lesion.site"))
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            for action in ("REVOKE", "UNDO"):
                field.refresh_from_db()
                revise_fact(patient, field.pk, actor=actor, action=action, expected_revision=field.revision_number,
                            expected_source=effective_fact(field)["current_source_token"], checked_original=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: undo_operation(patient, actor=actor, operation_id=operation.pk), pid_queue)
            waiter = pid_queue.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(ValidationError, match="来源"):
            future.result(timeout=15)
    field.refresh_from_db()
    assert effective_fact(field)["usable"] and not operation.reversed_by.exists()
    current = next(row for row in review_observations(patient, actor=actor) if row["id"] == selected["id"])
    assert current["status"] == "STALE" and not current["usable"]
