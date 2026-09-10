"""Actual PostgreSQL commit order for immutable pathology context review."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from apps.patients.access import change_membership
from apps.patients.models import PatientMembership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_detail_viewer import _patient
from tests.facts.pathology_factories import add_field, confirm_graph, ihc_fixture, review
from tests.facts.test_pathology_replacement import replacement_request
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated pathology PostgreSQL database")


@pytest.mark.parametrize("change", ["unselected_context", "author_purge", "editor_revoke"])
def test_waiting_group_replacement_rejects_committed_context_or_permission_change(django_user_model, change):
    _, patient, _, report, fields = ihc_fixture(django_user_model, "pg-pathology-context-" + change)
    _, collaborator = _patient(django_user_model, "pg-pathology-actor-" + change)
    actor = collaborator.account
    member = PatientMembership.objects.create(patient=patient, account=actor, role="EDITOR")
    if change == "author_purge":
        add_field(patient, report, "assay.method", "assay:a", {"code": "IHC", "raw": "合成方法"},
                  {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}, actor=actor, source_role="PRIMARY_ASSAY_METADATA")
    confirm_graph(patient, fields)
    changes = replacement_request(patient, report, fields)
    source = effective_fact(fields["tps"])["current_source_token"]
    expected_revision = fields["tps"].revision_number
    if change == "author_purge":
        job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    writer = actor if change == "editor_revoke" else patient.account
    count = report.fields.count()
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == "author_purge":
                assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
            elif change == "editor_revoke":
                change_membership(patient, patient.account, member.pk, revoke=True, expected_revision=0)
            else:
                review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-NEW"}, "raw_value": "SYN-NEW"})
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(patient, fields["tps"].pk, actor=writer,
                                 action="REPLACE_CONTEXT", expected_revision=expected_revision, expected_source=source,
                                 checked_original=True, changes=changes), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        # The actual author purge must COMMIT its deferred FKs while a reader
        # waits. An intentionally aborted purge would hide a lock-order defect.
        with pytest.raises(PermissionDenied if change == "editor_revoke" else FactConflict):
            future.result(timeout=20)
    assert report.fields.count() == count
    assert not report.fields.filter(revisions__after__has_key="context_replacement").exists()


def test_confirmation_serializes_with_whole_parent_exclusion(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import revise_report

    _, patient, _, report, fields = ihc_fixture(django_user_model, "pg-pathology-parent")
    field = fields["tps"]
    source = effective_fact(field)["current_source_token"]
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE", expected_revision=0,
                          expected_source=report_source_token(report))
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM",
                                 expected_revision=0, expected_source=source, checked_original=True), pids)
            wait_until_backend_is_blocked_by(state, request_pid=pids.get(timeout=10), blocker_pid=blocker)
        with pytest.raises(FactConflict):
            future.result(timeout=20)
    field.refresh_from_db()
    assert effective_fact(field)["status"] == "EXCLUDED" and not field.revisions.filter(action="CONFIRM").exists()
