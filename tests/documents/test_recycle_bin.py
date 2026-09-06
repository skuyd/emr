"""P3-AC12–15: reversible trash is distinct from permanent deletion."""

from datetime import timedelta
import uuid

import pytest
from django.utils import timezone

from apps.documents.deletion import purge_document_deletion, DeletionOutcome
from apps.documents.models import Document, DocumentDeletionJob, ProcessingRun, ProcessingStage
from apps.documents.quotas import _usage
from apps.operations.models import DeletionTombstone
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _parsed_document, _patient


pytestmark = pytest.mark.django_db


def test_regular_delete_retains_source_without_scheduling_permanent_deletion(django_user_model):
    client, patient = _patient(django_user_model, "trash")
    document, _a, _b = _parsed_document(patient)
    versions = set(document.parsing_versions.values_list("pk", flat=True))
    usage = _usage(patient)
    response = client.post(f"/records/{document.pk}/delete/", {"confirmation": "delete"})
    assert response.status_code == 302
    assert not DocumentDeletionJob.objects.filter(document=document).exists()
    assert not DeletionTombstone.objects.exists()
    document.refresh_from_db()
    assert document.trash_expires_at - document.trashed_at == timedelta(days=30)
    assert set(document.parsing_versions.values_list("pk", flat=True)) == versions
    assert _usage(patient) == (usage[0] - 1, usage[1] - document.page_count, usage[2])
    for path in (f"/records/{document.pk}/", f"/records/{document.pk}/original/", f"/records/{document.pk}/viewer/"):
        assert client.get(path).status_code == 404
    assert document.display_filename in client.get("/recycle-bin/").content.decode()


def test_restore_preserves_source_versions_and_storage_then_starts_a_new_retention_period(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document

    _client, patient = _patient(django_user_model, "retain")
    document, _a, _b = _parsed_document(patient)
    versions = list(document.parsing_versions.values_list("pk", flat=True))
    usage = _usage(patient)
    now = timezone.now()
    move_to_trash(patient, document.pk, now=now)
    restored = restore_document(patient, document.pk, now=now + timedelta(days=29))
    assert restored.deleted_at is None and restored.trashed_at is None
    assert restored.lifecycle_revision == 2
    assert list(restored.parsing_versions.values_list("pk", flat=True)) == versions
    assert _usage(patient) == usage
    again = move_to_trash(patient, document.pk, now=now + timedelta(days=29))
    assert again.trash_expires_at == now + timedelta(days=59)


@pytest.mark.parametrize("offset,allowed", [(timedelta(days=30, microseconds=-1), True), (timedelta(days=30), False), (timedelta(days=31), False)])
def test_restore_boundary_does_not_depend_on_cleanup_worker(django_user_model, offset, allowed):
    from apps.documents.lifecycle import LifecycleUnavailable, move_to_trash, restore_document

    _client, patient = _patient(django_user_model, "boundary")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    now = timezone.now()
    move_to_trash(patient, document.pk, now=now)
    if allowed:
        assert restore_document(patient, document.pk, now=now + offset).deleted_at is None
    else:
        with pytest.raises(LifecycleUnavailable, match="保留期"):
            restore_document(patient, document.pk, now=now + offset)
        assert Document.objects.filter(pk=document.pk, deleted_at__isnull=False).exists()


def test_duplicate_active_copy_blocks_restore_without_removing_either_source(django_user_model):
    from apps.documents.lifecycle import LifecycleUnavailable, move_to_trash, restore_document

    _client, patient = _patient(django_user_model, "duptrash")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    move_to_trash(patient, document.pk)
    copy, _pages = _document(patient, content_type="image/png", page_count=1)
    Document.objects.filter(pk=copy.pk).update(sha256=document.sha256)
    with pytest.raises(LifecycleUnavailable, match="相同内容"):
        restore_document(patient, document.pk)
    assert Document.objects.filter(pk=document.pk, trashed_at__isnull=False).exists()
    assert Document.objects.filter(pk=copy.pk, deleted_at__isnull=True).exists()


def test_restore_never_resumes_old_queued_or_leased_processing(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document
    from apps.processing.runner import ExecutionState, run_processing

    _client, patient = _patient(django_user_model, "fence")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    run = ProcessingRun.objects.create(document=document, parser_version="v1", task_type="parse", idempotency_key=str(uuid.uuid4()))
    move_to_trash(patient, document.pk)
    restore_document(patient, document.pk)
    invoked = []
    result = run_processing(run.pk, lambda context: invoked.append(context))
    assert result.state == ExecutionState.ALREADY_TERMINAL
    assert not invoked
    run.refresh_from_db()
    assert run.stage == ProcessingStage.FAILED and run.lease_token is None


def test_expiry_and_permanent_delete_hide_immediately_and_retry_full_object_cleanup(django_user_model):
    from apps.documents.lifecycle import LifecycleUnavailable, expire_trash, move_to_trash, restore_document

    _client, patient = _patient(django_user_model, "expire")
    document, pages = _document(patient, content_type="image/png", page_count=1)
    page = pages[0]
    page.image_object_key = f"originals/derived/{page.pk}/image"
    page.text_object_key = f"originals/derived/{page.pk}/text"
    page.save()
    now = timezone.now()
    move_to_trash(patient, document.pk, now=now)
    assert expire_trash(dispatch=lambda job: None, now=now + timedelta(days=30, microseconds=-1)) == ()
    assert expire_trash(dispatch=lambda job: None, now=now + timedelta(days=30)) == (document.pk,)
    with pytest.raises(LifecycleUnavailable):
        restore_document(patient, document.pk, now=now + timedelta(days=30))
    assert DeletionTombstone.objects.count() == 1
    job = DocumentDeletionJob.objects.get(document=document)
    store = InMemoryObjectStore()
    for key in (document.original_object_key, page.image_object_key, page.text_object_key):
        store.objects[key] = b"synthetic"
    store.fail_delete = True
    assert purge_document_deletion(job.pk, store).outcome == DeletionOutcome.RETRY_SCHEDULED
    assert Document.objects.filter(pk=document.pk, deleted_at__isnull=False).exists()
    store.fail_delete = False
    assert purge_document_deletion(job.pk, store).outcome == DeletionOutcome.PURGED
    assert store.objects == {}
    assert _usage(patient) == (0, 0, 0)


def test_trash_actions_require_owner_and_permanent_delete_requires_second_confirmation(django_user_model):
    from apps.documents.lifecycle import move_to_trash

    client, patient = _patient(django_user_model, "owner")
    other, _ = _patient(django_user_model, "intruder")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    move_to_trash(patient, document.pk)
    path = f"/recycle-bin/{document.pk}/delete/"
    assert other.get(path).status_code == 404
    assert other.post(f"/recycle-bin/{document.pk}/restore/").status_code == 404
    assert client.get(path).status_code == 200
    assert client.post(path, {}).status_code == 400
    assert not DocumentDeletionJob.objects.exists()
    assert client.post(path, {"confirmation": "permanent"}).status_code == 302
    assert DocumentDeletionJob.objects.filter(document=document).exists()
    assert client.post(f"/recycle-bin/{document.pk}/restore/").status_code == 404


def test_account_deletion_promotes_trash_to_permanent_without_waiting(django_user_model):
    from apps.accounts.deletion import request_account_deletion
    from apps.documents.lifecycle import LifecycleUnavailable, move_to_trash, restore_document

    _client, patient = _patient(django_user_model, "cancel")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    move_to_trash(patient, document.pk)
    request_account_deletion(patient.account_id, document_dispatch=lambda job: None, account_dispatch=lambda job: None)
    document.refresh_from_db()
    assert document.trashed_at is None and document.deleted_at is not None
    assert DocumentDeletionJob.objects.filter(document=document).exists()
    with pytest.raises(LifecycleUnavailable):
        restore_document(patient, document.pk)


def test_legacy_permanent_deletion_is_not_restorable(django_user_model):
    from apps.documents.deletion import request_document_deletion
    from apps.documents.lifecycle import LifecycleUnavailable, restore_document

    _client, patient = _patient(django_user_model, "legacy")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    job = request_document_deletion(patient, document.pk, dispatch=lambda job: None)
    with pytest.raises(LifecycleUnavailable):
        restore_document(patient, document.pk)
    assert purge_document_deletion(job.pk, InMemoryObjectStore()).outcome == DeletionOutcome.PURGED


def test_restore_preserves_lab_corrections_and_quality_but_never_regrants_reviews(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document
    from apps.labs.models import ReviewTaskStatus
    from apps.labs.review import create_review_task
    from apps.labs.revisions import effective_observation, revise_observation

    _client, patient = _patient(django_user_model, "trash-review")
    document, _a, _b = _parsed_document(patient)
    observation = document.parsing_versions.get(active=True).lab_observations.first()
    revise_observation(patient.account, observation.pk, action="REPORT_ERROR", changes={}, expected_revision=0)
    task = create_review_task(patient.account, observation.pk)
    move_to_trash(patient, document.pk)
    restore_document(patient, document.pk)
    observation.refresh_from_db()
    task.refresh_from_db()
    assert effective_observation(observation).reported_error
    assert observation.revision_number == 1
    assert task.status == ReviewTaskStatus.REVOKED and task.revoked_at is not None
    assert task.events.order_by("-sequence").first().action == "DOCUMENT_TRASHED"


def test_permanent_tombstone_replay_overrides_a_restored_trash_snapshot(django_user_model):
    from apps.documents.lifecycle import LifecycleUnavailable, move_to_trash, restore_document
    from apps.operations.models import TombstoneKind
    from apps.operations.tombstones import record_deletion_tombstone, export_tombstone_entries, replay_restore_tombstones

    _client, patient = _patient(django_user_model, "trash-replay")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    move_to_trash(patient, document.pk)
    record_deletion_tombstone(TombstoneKind.DOCUMENT, document.pk)
    entries = export_tombstone_entries()
    result = replay_restore_tombstones(entries, document_dispatch=lambda job: None, account_dispatch=lambda job: None)
    assert result.documents_hidden == 1
    assert DocumentDeletionJob.objects.filter(document=document).exists()
    with pytest.raises(LifecycleUnavailable):
        restore_document(patient, document.pk)


def test_permanent_cleanup_erases_original_and_page_objects_in_the_real_local_adapter(django_user_model, tmp_path):
    import hashlib
    import io
    from apps.documents.deletion import request_document_deletion
    from apps.documents.storage import LocalObjectStore

    _, patient = _patient(django_user_model, "trash-local-store")
    document, pages = _document(patient, content_type="image/png", page_count=1)
    pages[0].image_object_key = f"originals/derived/{pages[0].pk}/image"
    pages[0].text_object_key = f"originals/derived/{pages[0].pk}/text"
    pages[0].save()
    store = LocalObjectStore(tmp_path)
    content = b"synthetic-source-and-page"
    for key in (document.original_object_key, pages[0].image_object_key, pages[0].text_object_key):
        staged = store.put_staging(io.BytesIO(content), expected_size=len(content), expected_sha256=hashlib.sha256(content).hexdigest())
        store.promote_immutable(staged, key)
    job = request_document_deletion(patient, document.pk, dispatch=lambda job: None)
    assert purge_document_deletion(job.pk, store).outcome == DeletionOutcome.PURGED
    assert not any(path.is_file() for path in tmp_path.rglob("*"))
