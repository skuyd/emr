"""Patient selection and family permissions must hold at existing interfaces."""

import pytest
from django.test import Client

from apps.patients.models import Patient
from tests.documents.test_detail_viewer import _document, _patient


pytestmark = pytest.mark.django_db


def test_create_second_patient_and_explicit_switch_preserve_first_archive(django_user_model):
    client, first = _patient(django_user_model, "family-create")
    document, _ = _document(first)
    response = client.post("/patients/new/", {"display_name": "另一位家人", "upload_authority": "on"})
    assert response.status_code == 302
    second = Patient.objects.exclude(pk=first.pk).get(account=first.account)
    assert client.get("/records/").context["request"].patient.pk == second.pk
    assert client.get(f"/records/{document.pk}/").status_code == 200
    assert client.get(f"/records/{document.pk}/", {"patient": str(second.pk)}).status_code == 404
    assert client.post(f"/patients/{first.pk}/select/").status_code == 302
    assert client.get(f"/records/{document.pk}/").status_code == 200


def test_old_tab_cannot_rename_another_patient_after_switch(django_user_model):
    client, first = _patient(django_user_model, "family-tab")
    response = client.post("/patients/new/", {"display_name": "第二位", "upload_authority": "on"})
    assert response.status_code == 302
    second = Patient.objects.exclude(pk=first.pk).get(account=first.account)
    result = client.post("/me/name/", {"display_name": "旧页面修改", "patient_id": str(first.pk)})
    assert result.status_code in {302, 409}
    second.refresh_from_db()
    assert second.display_name == "第二位"
    if result.status_code == 302:
        assert client.get(result.url).context["request"].patient.pk == first.pk


def test_family_viewer_reads_existing_routes_but_cannot_write(django_user_model):
    from apps.patients.models import PatientMembership

    _, patient = _patient(django_user_model, "family-owner")
    viewer_client, own = _patient(django_user_model, "family-reader")
    PatientMembership.objects.create(patient=patient, account=own.account, role="VIEWER")
    document, _ = _document(patient)
    viewer_client.post(f"/patients/{patient.pk}/select/")
    assert viewer_client.get(f"/records/{document.pk}/").status_code == 200
    assert viewer_client.post(f"/records/{document.pk}/delete/", {"confirmation": "delete"}).status_code == 403
    assert viewer_client.post("/me/name/", {"display_name": "forged"}).status_code == 403
    document.refresh_from_db()
    assert document.deleted_at is None


def test_multiple_available_patients_require_selection_and_revocation_is_fresh(django_user_model):
    from apps.patients.models import PatientMembership

    _, patient = _patient(django_user_model, "family-scope-owner")
    client, own = _patient(django_user_model, "family-scope-member")
    membership = PatientMembership.objects.create(patient=patient, account=own.account, role="EDITOR")
    assert client.get("/").status_code == 302
    assert client.post(f"/patients/{patient.pk}/select/").status_code == 302
    assert client.get("/records/").status_code == 200
    from django.utils import timezone
    PatientMembership.objects.filter(pk=membership.pk).update(revoked_at=timezone.now())
    assert client.get("/records/").status_code in {302, 404}


def family(django_user_model, marker, role="EDITOR"):
    from apps.patients.models import PatientMembership
    owner_client, patient = _patient(django_user_model, marker + "-owner")
    client, own = _patient(django_user_model, marker + "-member")
    membership = PatientMembership.objects.create(patient=patient, account=own.account, role=role)
    client.post(f"/patients/{patient.pk}/select/")
    client.get("/records/")
    return owner_client, patient, client, own.account, membership


def test_editor_fact_revision_records_real_member_and_rechecks_revocation(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.revisions import revise_fact
    from apps.patients.access import change_membership
    from tests.facts.factories import parsed_facts
    from django.core.exceptions import PermissionDenied

    _, patient, client, actor, membership = family(django_user_model, "family-facts")
    _, version = parsed_facts(patient, ["诊断：合成诊断。"])
    fact = Fact.objects.get(parsing_version=version)
    token = client.get(f"/facts/{fact.pk}/").context["row"]["current_source_token"]
    response = client.post(f"/facts/{fact.pk}/", {
        "patient_id": str(patient.pk), "action": "DEFER", "expected_revision": 0,
        "category": "DIAGNOSIS", "text": "诊断：合成诊断。", "expected_source": token,
    })
    assert response.status_code == 302
    assert fact.revisions.get().author_id == actor.pk
    change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    with pytest.raises(PermissionDenied):
        revise_fact(patient, fact.pk, actor=actor, action="DEFER", expected_revision=1)


def test_viewer_lab_sources_are_readable_and_all_owner_mutations_are_denied(django_user_model):
    from datetime import date
    from tests.labs.test_trends import _observation

    _, patient, client, _, _ = family(django_user_model, "family-labs", "VIEWER")
    _, row = _observation(patient, date(2026, 8, 1), "4")
    assert client.get(f"/labs/observations/{row.pk}/").status_code == 200
    assert client.get(f"/labs/observations/{row.pk}/source/raw_value/").status_code == 200
    for url in (f"/labs/observations/{row.pk}/", f"/labs/observations/{row.pk}/review/",
                f"/labs/versions/{row.parsing_version_id}/activate/", "/visit/"):
        assert client.post(url, {"patient_id": str(patient.pk)}).status_code == 403
    row.refresh_from_db()
    assert row.revision_number == 0


def test_member_export_is_bound_to_actor_and_revocation_invalidates_running_result(django_user_model):
    from apps.exports.services import create_preview, request_generation, generate_export, download_export
    from apps.exports.errors import ExportUnavailable
    from apps.patients.access import change_membership
    from tests.documents.fakes import InMemoryObjectStore

    owner_client, patient, client, actor, membership = family(django_user_model, "family-export")
    _document(patient)
    key = client.session.session_key
    job = create_preview(patient, key, {"mode": "all"}, actor=actor)
    assert job.requested_by_id == actor.pk
    request_generation(patient, key, job.pk, {"format": "json"}, actor=actor, dispatch=lambda _: None)
    assert owner_client.get(f"/visit/{job.pk}/").status_code == 404
    assert owner_client.get(f"/visit/?edit={job.pk}").status_code == 404
    change_membership(patient, patient.account, membership.pk, role="VIEWER", expected_revision=0)
    store = InMemoryObjectStore()
    generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {}
    assert not store.objects
    with pytest.raises(ExportUnavailable):
        download_export(patient, key, job.pk, store, actor=actor)


def test_original_stream_stops_when_member_is_revoked(django_user_model, monkeypatch):
    from apps.patients.access import change_membership
    from tests.documents.fakes import InMemoryObjectStore

    _, patient, client, _, membership = family(django_user_model, "family-stream")
    document, _ = _document(patient)
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"synthetic original" + b"x" * 1024 * 1024
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    response = client.get(f"/records/{document.pk}/original/")
    assert response.status_code == 200
    stream = iter(response.streaming_content)
    assert next(stream)
    change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
    assert list(stream) == []


def test_original_stream_bounds_authorization_queries_for_one_megabyte(django_user_model, monkeypatch):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from tests.documents.fakes import InMemoryObjectStore

    _, patient, client, _, _ = family(django_user_model, "family-stream-volume")
    document, _ = _document(patient)
    store = InMemoryObjectStore()
    payload = b"x" * 1024 * 1024
    store.objects[document.original_object_key] = payload
    monkeypatch.setattr("apps.documents.views.originals.get_object_store", lambda: store)
    response = client.get(f"/records/{document.pk}/original/")
    with CaptureQueriesContext(connection) as queries:
        chunks = list(response.streaming_content)
    assert b"".join(chunks) == payload
    assert len(chunks) <= 8 and len(queries) < 100


def test_notification_read_and_preferences_are_member_specific(django_user_model):
    from apps.notifications.models import TaskNotification
    from tests.notifications.test_services import make_completed_batch
    from apps.patients.models import PatientPreference

    owner_client, patient, client, actor, _ = family(django_user_model, "family-notify")
    batch = make_completed_batch(patient)
    note = TaskNotification.objects.create(patient=patient, batch=batch, kind="COMPLETED")
    assert client.post(f"/api/notifications/{note.pk}/read/", {"patient_id": str(patient.pk)}).status_code == 200
    assert client.get("/api/notifications/").json()["unread_count"] == 0
    assert owner_client.get("/api/notifications/").json()["unread_count"] == 1
    result = client.post("/me/notifications/", {
        "patient_id": str(patient.pk), "enabled": "false", "prompted": "on",
    })
    assert result.status_code == 302
    assert PatientPreference.objects.get(patient=patient, account=actor).browser_notifications_enabled is False


def test_admin_cannot_revoke_creator_or_grant_admin(django_user_model):
    from apps.patients.models import PatientMembership
    from apps.patients.access import change_membership
    from django.core.exceptions import PermissionDenied

    _, patient, _, actor, membership = family(django_user_model, "family-manager", "ADMIN")
    creator = PatientMembership.objects.get(patient=patient, account=patient.account)
    with pytest.raises(PermissionDenied):
        change_membership(patient, actor, creator.pk, revoke=True, expected_revision=0)
    with pytest.raises(PermissionDenied):
        change_membership(patient, actor, membership.pk, role="VIEWER", expected_revision=0)


def test_account_deletion_cleans_all_owned_patients_but_retains_joined_documents(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion
    from apps.patients.models import PatientMembership

    _, shared, _, actor, membership = family(django_user_model, "family-deletion")
    document, _ = _document(shared)
    first = Patient.objects.get(account=actor)
    second = Patient.objects.create(account=actor, display_name="另一位")
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    assert Patient.objects.filter(pk__in=[first.pk, second.pk], deleted_at__isnull=False).count() == 2
    assert PatientMembership.objects.get(pk=membership.pk).revoked_at is not None
    document.refresh_from_db()
    assert document.deleted_at is None
    assert purge_account_deletion(job.pk).outcome == "PURGED"
    assert Patient.objects.filter(account_id=actor.pk).count() == 0
    assert Patient.objects.filter(pk=shared.pk).exists()


def test_member_reprocessing_cannot_publish_after_role_downgrade(django_user_model):
    from django.db import transaction
    from apps.documents.models import DocumentStatus
    from apps.patients.access import change_membership
    from apps.processing.reprocessing import queue_user_reprocessing
    from apps.processing.runner import run_processing, PipelineResult, ExecutionState

    _, patient, _, actor, membership = family(django_user_model, "family-reprocessing")
    document, _ = _document(patient, status=DocumentStatus.PROCESSING_FAILED)
    run = queue_user_reprocessing(patient, document.pk, actor=actor, dispatch=lambda _: None)
    assert run.requested_by_id == actor.pk

    def pipeline(context):
        change_membership(patient, patient.account, membership.pk, role="VIEWER", expected_revision=0)
        with transaction.atomic():
            context.assert_current()
        return PipelineResult.organized()

    assert run_processing(run.pk, pipeline).state == ExecutionState.LEASE_LOST
    run.refresh_from_db()
    document.refresh_from_db()
    assert run.stage == "FAILED" and run.error_code == "access_revoked"
    assert not run.is_current and document.status == DocumentStatus.PROCESSING_FAILED


def test_deleted_patient_cannot_acquire_processing_lease(django_user_model):
    from django.utils import timezone
    from tests.processing.test_runner import make_run
    from apps.processing.runner import run_processing, ExecutionState

    _, document, run = make_run(django_user_model)
    Patient.objects.filter(pk=document.patient_id).update(deleted_at=timezone.now())
    calls = []
    assert run_processing(run.pk, lambda _: calls.append(True)).state == ExecutionState.LEASE_LOST
    assert calls == []


def test_deleting_last_patient_returns_to_creation_and_admin_cannot_delete(django_user_model):
    owner_client, patient, admin, _, _ = family(django_user_model, "family-last-delete", "ADMIN")
    assert admin.post(f"/patients/{patient.pk}/delete/", {"confirmation": "delete-patient"}).status_code == 403
    assert owner_client.post(f"/patients/{patient.pk}/delete/", {"confirmation": "delete-patient"}).status_code == 302
    result = owner_client.get("/")
    assert result.status_code == 302 and result.url == "/patients/new/"


def test_admin_review_grant_is_revoked_with_real_revoking_actor(django_user_model):
    from datetime import date
    from django.core.exceptions import PermissionDenied
    from apps.labs.review import create_review_task, get_review_task
    from apps.patients.access import change_membership
    from tests.labs.test_phase_two_workflows import _reviewer
    from tests.labs.test_trends import _observation

    owner_client, patient, _, admin, membership = family(django_user_model, "family-review", "ADMIN")
    _, row = _observation(patient, date(2026, 8, 1), "4")
    reviewer = _reviewer(django_user_model)
    task = create_review_task(admin, row.pk, reviewer=reviewer)
    assert get_review_task(reviewer, task.pk).pk == task.pk
    assert owner_client.get(f"/labs/reviews/{task.pk}/").context["owner"] is True
    change_membership(patient, patient.account, membership.pk, role="EDITOR", expected_revision=0)
    with pytest.raises(PermissionDenied):
        get_review_task(reviewer, task.pk)
    assert task.events.get(action="MEMBER_REVOKED").author_id == patient.account_id


def test_shared_upload_records_member_on_document_and_initial_work(django_user_model, monkeypatch):
    from tests.documents.test_upload_views import reserve_one, upload_path, uploaded_png
    from tests.documents.fakes import InMemoryObjectStore
    from apps.documents.models import Document

    _, patient, client, actor, _ = family(django_user_model, "family-upload")
    client.defaults["HTTP_X_PATIENT_ID"] = str(patient.pk)
    store = InMemoryObjectStore()
    monkeypatch.setattr("apps.documents.views.uploads.get_object_store", lambda: store)
    batch_id, item_id = reserve_one(client)
    response = client.post(upload_path(batch_id, item_id), {"file": uploaded_png()})
    assert response.status_code == 201
    document = Document.objects.get(pk=response.json()["document_id"])
    assert document.patient_id == patient.pk and document.created_by_id == actor.pk
    assert document.batch.created_by_id == actor.pk
    assert document.processing_runs.get().requested_by_id == actor.pk


def test_inflight_onboarding_cannot_recreate_patient_after_account_deletion(django_user_model):
    from django.core.exceptions import PermissionDenied
    from apps.accounts.deletion import request_account_deletion
    from apps.patients.services import create_patient_space
    from tests.documents.test_detail_viewer import CONFIRMATIONS, EVIDENCE

    _, patient = _patient(django_user_model, "family-onboarding-deletion")
    stale_actor = patient.account
    request_account_deletion(stale_actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    with pytest.raises(PermissionDenied):
        create_patient_space(stale_actor, "删除竞争", CONFIRMATIONS, EVIDENCE)
    assert Patient.objects.filter(account=stale_actor, deleted_at__isnull=True).count() == 0


@pytest.mark.parametrize(("role", "allowed"), [
    ("OWNER", {"read", "write", "export", "manage", "owner"}),
    ("ADMIN", {"read", "write", "export", "manage"}),
    ("EDITOR", {"read", "write", "export"}),
    ("VIEWER", {"read"}),
    ("NONE", set()),
])
def test_role_capability_matrix_uses_current_patient_membership(django_user_model, role, allowed):
    from django.core.exceptions import PermissionDenied
    from apps.patients.access import authorize_patient, Capability

    _, patient, _, actor, membership = family(django_user_model, "family-matrix-" + role, "ADMIN" if role in {"OWNER", "NONE"} else role)
    if role == "OWNER":
        actor = patient.account
    elif role == "NONE":
        membership.delete()
    for capability in Capability:
        if capability.value in allowed:
            assert authorize_patient(patient, actor, capability).actor.pk == actor.pk
        else:
            with pytest.raises(PermissionDenied):
                authorize_patient(patient, actor, capability)
