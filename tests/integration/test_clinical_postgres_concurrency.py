"""Real PostgreSQL locks must fence stale clinical review and export work."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import revise_report
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.facts.test_clinical_foundation import CT, clinical_fixture
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")


def test_parent_exclusion_serializes_with_pending_field_confirmation(django_user_model):
    _, patient, document, _, _ = clinical_fixture(django_user_model, name="pg-clinical-parent")
    report = document.clinical_reports.get()
    field = report.fields.get(field_key="imaging.impression")
    source = effective_fact(field)["current_source_token"]
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revision = revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE",
                                     expected_revision=0, expected_source=report_source_token(report))
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(
                patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=source, checked_original=True), pid_queue)
            waiter = pid_queue.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(FactConflict):
            future.result(timeout=15)
    fields = list(report.fields.all())
    assert len(revision.field_revisions) == len(fields)
    assert all(item.revision_number == 1 and effective_fact(item)["status"] == "EXCLUDED" for item in fields)
    assert not field.revisions.filter(action="CONFIRM").exists()


def test_parse_activation_fences_confirmation_waiting_on_document_lock(django_user_model):
    from apps.processing.models import ParsingVersion

    _, patient, document, first, _ = clinical_fixture(django_user_model, name="pg-clinical-version")
    field = first.facts.get(field_key="imaging.impression")
    _, _, _, second, _ = clinical_fixture(django_user_model, document=document, previous=first)
    first = ParsingVersion.objects.activate(first.pk)
    # Use the current activation identity before the competing publication starts.
    field.refresh_from_db()
    source = effective_fact(field)["current_source_token"]
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            ParsingVersion.objects.activate(second.pk)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(
                patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=source, checked_original=True), pid_queue)
            waiter = pid_queue.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(FactConflict):
            future.result(timeout=15)
    field.refresh_from_db()
    assert not field.revisions.exists() and not effective_fact(field)["usable"]
    assert second.facts.filter(representation="FIELD", revision_number=0).exists()


def test_revoked_editor_cannot_commit_a_waiting_clinical_revision(django_user_model):
    from apps.facts.clinical_extraction import extract_clinical_version
    from apps.patients.access import change_membership
    from tests.facts.factories import parsed_facts
    from tests.patients.test_family_access import family

    _, patient, _, actor, membership = family(django_user_model, "pg-clinical-revoke")
    _, version = parsed_facts(patient, CT, document_type="IMAGING")
    extract_clinical_version(version)
    field = version.facts.get(field_key="imaging.impression")
    source = effective_fact(field)["current_source_token"]
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(
                patient, field.pk, actor=actor, action="CONFIRM", expected_revision=0,
                expected_source=source, checked_original=True), pid_queue)
            waiter = pid_queue.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(PermissionDenied):
            future.result(timeout=15)
    assert not field.revisions.exists()


@pytest.mark.parametrize("change", ["field", "parent"])
def test_clinical_change_during_export_build_prevents_publication(django_user_model, monkeypatch, change):
    from apps.exports import services
    from tests.documents.fakes import InMemoryObjectStore

    client, patient, document, _, _ = clinical_fixture(django_user_model, name="pg-clinical-export-" + change)
    field = document.facts.get(field_key="imaging.impression")
    revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(field)["current_source_token"], checked_original=True)
    assert client.get("/records/").status_code == 200
    job = services.create_preview(patient, client.session.session_key, {"mode": "all"}, actor=patient.account)
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"},
                                actor=patient.account, dispatch=lambda _: None)
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
            if change == "field":
                field.refresh_from_db()
                revise_fact(patient, field.pk, actor=patient.account, action="REVOKE", expected_revision=1,
                            expected_source=effective_fact(field)["current_source_token"])
            else:
                report = document.clinical_reports.get()
                revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE",
                              expected_revision=0, expected_source=report_source_token(report))
        finally:
            release.set()
        future.result(timeout=15)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {} and store.objects == {}
