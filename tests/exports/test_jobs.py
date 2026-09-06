from datetime import timedelta
import hashlib

import pytest
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.documents.lifecycle import move_to_trash, restore_document
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def _preview(django_user_model, marker):
    from apps.exports.services import create_preview

    client, patient = _patient(django_user_model, marker)
    client.get("/records/")
    document, version = parsed_facts(patient, ["诊断：考虑炎症。"])
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    job = create_preview(patient, client.session.session_key, {"mode": "all"})
    return client, patient, document, fact, job


def _ready(patient, client, job, store):
    from apps.exports.services import request_generation, generate_export

    queued = []
    request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=queued.append)
    generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == "READY"
    return job


def test_same_preview_is_generated_private_and_download_scope_is_rechecked(django_user_model):
    from apps.exports.services import download_export

    client, patient, _, _, job = _preview(django_user_model, "job-download")
    store = InMemoryObjectStore()
    snapshot = job.snapshot
    _ready(patient, client, job, store)
    assert job.expires_at == job.completed_at + timedelta(hours=24)
    artifact = download_export(patient, client.session.session_key, job.pk, store)
    assert hashlib.sha256(artifact.payload).hexdigest() == job.sha256
    assert job.snapshot == snapshot
    assert not any(call[0] == "presign_get" for call in store.calls)
    foreign_client, foreign = _patient(django_user_model, "job-foreign")
    with pytest.raises(PermissionDenied):
        download_export(foreign, foreign_client.session.session_key, job.pk, store)


def test_revoke_and_trash_restore_make_old_ready_result_unavailable_and_cleanable(django_user_model):
    from apps.exports.errors import ExportUnavailable
    from apps.exports.services import cleanup_export, download_export

    client, patient, document, fact, job = _preview(django_user_model, "job-invalidate")
    store = InMemoryObjectStore()
    _ready(patient, client, job, store)
    revise_fact(patient, fact.pk, action="REVOKE", expected_revision=1)
    with pytest.raises(ExportUnavailable):
        download_export(patient, client.session.session_key, job.pk, store)
    job.refresh_from_db()
    assert job.status == "INVALIDATED"
    assert cleanup_export(job.pk, store)
    assert not store.objects
    move_to_trash(patient, document.pk)
    restore_document(patient, document.pk)
    with pytest.raises(ExportUnavailable):
        download_export(patient, client.session.session_key, job.pk, store)


def test_expiry_is_exact_and_storage_failure_stays_hidden_until_cleanup_retry(django_user_model):
    from apps.exports.errors import ExportUnavailable
    from apps.exports.services import cleanup_export, download_export

    client, patient, _, _, job = _preview(django_user_model, "job-expiry")
    store = InMemoryObjectStore()
    _ready(patient, client, job, store)
    session = client.session
    session["session_last_seen_at"] = int((job.expires_at - timedelta(minutes=1)).timestamp())
    session.save()
    download_export(patient, client.session.session_key, job.pk, store, now=job.expires_at - timedelta(microseconds=1))
    with pytest.raises(ExportUnavailable):
        download_export(patient, client.session.session_key, job.pk, store, now=job.expires_at)
    store.fail_delete = True
    assert not cleanup_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == "EXPIRED" and job.cleanup_pending
    store.fail_delete = False
    assert cleanup_export(job.pk, store)
    assert not store.objects


def test_logged_out_originating_session_cannot_generate_or_download(django_user_model):
    from apps.exports.errors import ExportUnavailable
    from apps.exports.services import generate_export, request_generation

    client, patient, _, _, job = _preview(django_user_model, "job-session")
    key = client.session.session_key
    request_generation(patient, key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    Session.objects.filter(session_key=key).delete()
    generate_export(job.pk, InMemoryObjectStore())
    job.refresh_from_db()
    assert job.status == "INVALIDATED"
    with pytest.raises(ExportUnavailable):
        request_generation(patient, key, job.pk, {"format": "json"}, dispatch=lambda _: None)


def test_failure_can_retry_same_snapshot_and_cancel_fences_worker(django_user_model, monkeypatch):
    from apps.exports import services

    client, patient, _, _, job = _preview(django_user_model, "job-retry")
    store = InMemoryObjectStore()
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    store.fail_promote = True
    services.generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == "FAILED" and job.failures and job.cleanup_pending
    snapshot = job.snapshot
    store.fail_promote = False
    assert services.cleanup_export(job.pk, store)
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    original = services.build_artifact
    def cancel_while_building(*args, **kwargs):
        artifact = original(*args, **kwargs)
        services.cancel_export(patient, client.session.session_key, job.pk)
        return artifact
    monkeypatch.setattr(services, "build_artifact", cancel_while_building)
    services.generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == "CANCELLED"
    assert services.cleanup_export(job.pk, store)
    assert not store.objects
    assert snapshot["facts"]


def test_source_edit_during_generation_cannot_publish(django_user_model, monkeypatch):
    from apps.exports import services

    client, patient, _, fact, job = _preview(django_user_model, "job-mid-edit")
    store = InMemoryObjectStore()
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    original = services.build_artifact
    def edit_during_build(*args, **kwargs):
        artifact = original(*args, **kwargs)
        revise_fact(patient, fact.pk, action="REVOKE", expected_revision=1)
        return artifact
    monkeypatch.setattr(services, "build_artifact", edit_during_build)
    services.generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == "INVALIDATED"
    services.cleanup_export(job.pk, store)
    assert not store.objects


def test_staging_response_loss_is_reclaimed_by_durable_planned_key(django_user_model):
    from apps.documents.storage import S3ObjectStore
    from apps.exports import services
    from tests.documents.fakes import VersionedS3Client

    client, patient, _, _, job = _preview(django_user_model, "job-lost-response")
    s3 = VersionedS3Client()
    store = S3ObjectStore(s3, "private-synthetic")
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    planned = job.attempts.get()
    s3.write_then_lose_put_response_once()
    services.generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == "FAILED" and job.cleanup_pending
    assert planned.staging_key in s3.objects
    assert services.cleanup_export(job.pk, store)
    assert not s3.objects


def test_permanent_source_cleanup_waits_for_export_objects_and_scrubs_snapshot(django_user_model):
    from apps.documents.deletion import DeletionOutcome, purge_document_deletion, request_document_deletion
    from apps.exports.models import ExportJob

    client, patient, document, _, job = _preview(django_user_model, "job-source-purge")
    store = InMemoryObjectStore()
    _ready(patient, client, job, store)
    deletion = request_document_deletion(patient, document.pk, dispatch=lambda _: None)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {}
    store.fail_delete = True
    assert purge_document_deletion(deletion.pk, store).outcome == DeletionOutcome.RETRY_SCHEDULED
    assert type(document).objects.filter(pk=document.pk).exists()
    store.fail_delete = False
    assert purge_document_deletion(deletion.pk, store).outcome == DeletionOutcome.PURGED
    assert not store.objects
    assert ExportJob.objects.get(pk=job.pk).snapshot == {}


def test_account_purge_waits_for_cancelled_export_without_sources(django_user_model):
    from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
    from apps.documents.deletion import purge_document_deletion
    from apps.exports import services
    from apps.exports.models import ExportJob

    client, patient, document, _, job = _preview(django_user_model, "job-account-purge")
    store = InMemoryObjectStore()
    _ready(patient, client, job, store)
    services.cancel_export(patient, client.session.session_key, job.pk)
    account_job = request_account_deletion(patient.account_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    document.refresh_from_db()
    # Simulate a completed source unlink while export cleanup still has its durable identities.
    job.source_bindings.all().delete()
    purge_document_deletion(document.deletion_job.pk, store)
    assert purge_account_deletion(account_job.pk).outcome == AccountDeletionOutcome.RETRY_SCHEDULED
    assert services.cleanup_export(job.pk, store)
    assert purge_account_deletion(account_job.pk).outcome == AccountDeletionOutcome.PURGED
    assert not ExportJob.objects.filter(pk=job.pk).exists()
    assert not store.objects


def test_recovery_expires_previews_and_releases_lost_worker_lease(django_user_model):
    from apps.exports import services
    from apps.exports.models import ExportJob
    import uuid

    client, patient, _, _, job = _preview(django_user_model, "job-recovery")
    store = InMemoryObjectStore()
    services.request_generation(patient, client.session.session_key, job.pk, {"format": "json"}, dispatch=lambda _: None)
    ExportJob.objects.filter(pk=job.pk).update(
        status="GENERATING", lease_token=uuid.uuid4(), lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    services.recover_exports(store, dispatch=lambda _: None)
    job.refresh_from_db()
    assert job.status == "FAILED" and not job.cleanup_pending
    services.recover_exports(store, dispatch=lambda _: None, now=job.expires_at)
    job.refresh_from_db()
    assert job.status == "EXPIRED" and job.snapshot == {}


def test_deleted_excluded_source_also_removes_its_frozen_preview_metadata(django_user_model):
    from apps.exports.services import create_preview
    from tests.documents.test_detail_viewer import _document

    client, patient, selected, _, _ = _preview(django_user_model, "job-excluded")
    excluded, _ = _document(patient)
    job = create_preview(patient, client.session.session_key, {"mode": "documents", "document_ids": [str(selected.pk)]})
    assert job.snapshot["excluded_documents"][0]["filename"] == excluded.display_filename
    move_to_trash(patient, excluded.pk)
    job.refresh_from_db()
    assert job.status == "INVALIDATED" and job.snapshot == {}
