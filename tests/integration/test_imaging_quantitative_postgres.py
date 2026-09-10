"""Typed scalar confirmation retains the patient/document write fences."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction

from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import revise_report
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.facts.factories import parsed_facts
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("change", ["parent", "membership"])
def test_waiting_suv_confirmation_cannot_outlive_parent_or_editor(django_user_model, change):
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")
    from apps.patients.access import change_membership

    _, patient, _, actor, membership = family(django_user_model, "pg-imaging-" + change)
    document, version = parsed_facts(patient, [
        "合成医院 PET/CT诊断报告书", "检查日期：2026-08-17 检查项目：全身PET/CT",
        "影像表现：左肺上叶结节约12mm，SUVmax≤4.20。", "诊断意见：请结合原件核对。",
    ], document_type="IMAGING")
    extract_clinical_version(version)
    field = version.facts.get(field_key="lesion.suvmax")
    source = effective_fact(field)["current_source_token"]
    assert field.schema_version == "1.1"
    assert field.automatic_content["value"]["comparator"] == "LE"
    pid_queue = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == "parent":
                report = document.clinical_reports.get()
                revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE",
                              expected_revision=0, expected_source=report_source_token(report))
            else:
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(
                patient, field.pk, actor=actor, action="CONFIRM", expected_revision=0,
                expected_source=source, checked_original=True), pid_queue)
            waiter = pid_queue.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(FactConflict if change == "parent" else PermissionDenied):
            future.result(timeout=15)
    field.refresh_from_db()
    assert not field.revisions.filter(action="CONFIRM").exists()
    assert not effective_fact(field)["usable"]


@pytest.mark.parametrize("change", ["field", "record"])
def test_mixed_portable_publication_rechecks_clinical_and_daily_sources(django_user_model, monkeypatch, change):
    if connection.vendor != "postgresql":
        pytest.skip("Requires the isolated PostgreSQL test database")
    from apps.exports import services
    from apps.self_records.services import create_record, revise_record
    from tests.documents.fakes import InMemoryObjectStore
    from tests.facts.test_imaging_quantitative import imaging
    from tests.self_records.test_payloads import payload

    client, patient, document, _, _ = imaging(django_user_model,
        "左肺结节约12mm，SUVmax≤4.20。", name="pg-mixed-imaging-" + change)
    field = document.facts.get(field_key="lesion.suvmax")
    revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(field)["current_source_token"], checked_original=True)
    record = create_record(patient, patient.account, payload(), creation_key=uuid4()).record
    assert client.get("/visit/").status_code == 200
    selection = {"mode": "documents", "document_ids": [str(document.pk)], "clinical_field_ids": [str(field.pk)],
                 "self_record_ids": [str(record.pk)], "sections": ["imaging", "self_records"]}
    job = services.create_preview(patient, client.session.session_key, selection, actor=patient.account)
    assert job.snapshot["schema_version"] == "1.5"
    assert len(job.snapshot["clinical_fields"]) == len(job.snapshot["self_records"]) == 1
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
    store, pid_queue = InMemoryObjectStore(), Queue()
    owner_pid = backend_pid()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: services.generate_export(job.pk, store), pid_queue)
        assert pid_queue.get(timeout=10) != owner_pid
        try:
            assert entered.wait(timeout=10)
            if change == "field":
                field.refresh_from_db()
                revise_fact(patient, field.pk, actor=patient.account, action="REVOKE", expected_revision=1,
                            expected_source=effective_fact(field)["current_source_token"])
            else:
                revise_record(patient, patient.account, record.pk, action="CORRECT", expected_revision=0,
                              changes=payload(value="61"))
        finally:
            release.set()
        future.result(timeout=15)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {} and store.objects == {}
