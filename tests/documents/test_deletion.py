from datetime import timedelta
import uuid

from django.utils import timezone
import pytest

from apps.documents.deletion import (
    DeletionOutcome,
    due_document_deletions,
    purge_document_deletion,
    request_document_deletion,
)
from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentDeletionJob,
    DocumentStatus,
    ProcessingRun,
    InaccuracyFeedback,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.labs.models import LabObservation
from apps.processing.models import ParsingVersion
from tests.documents.fakes import InMemoryObjectStore, VersionedS3Client
from apps.documents.storage import S3ObjectStore
from tests.documents.test_detail_viewer import _document, _parsed_document, _patient


pytestmark = pytest.mark.django_db


def _trace_locked_rows(monkeypatch):
    from django.db.models.query import QuerySet

    acquired = []
    original = QuerySet._fetch_all

    def fetch(queryset):
        first_evaluation = queryset._result_cache is None
        original(queryset)
        if first_evaluation and queryset.query.select_for_update:
            acquired.extend((queryset.model, row.pk) for row in queryset._result_cache)

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    return acquired


@pytest.mark.parametrize("operation", ["request", "purge", "reprocess"])
def test_document_mutations_lock_ordered_batches_before_document(monkeypatch, django_user_model, operation):
    """The lock requests themselves retain their order even on SQLite."""
    from apps.processing.reprocessing import queue_user_reprocessing

    _client, patient = _patient(django_user_model, "l")
    document, _pages = _document(patient, content_type="image/png", page_count=1, status=DocumentStatus.PROCESSING_FAILED)
    other_batch = UploadBatch.objects.create(pk=uuid.UUID(int=1), patient=patient)
    UploadItem.objects.create(
        batch=other_batch, ordinal=1, display_filename="duplicate.png",
        status=UploadItemStatus.EXACT_DUPLICATE, document=document,
    )
    store = InMemoryObjectStore()
    job = None
    if operation == "purge":
        job = request_document_deletion(patient, document.pk, dispatch=lambda _job: None)
    locks = _trace_locked_rows(monkeypatch)

    if operation == "request":
        request_document_deletion(patient, document.pk, dispatch=lambda _job: None)
    elif operation == "purge":
        purge_document_deletion(job.pk, store)
    else:
        queue_user_reprocessing(patient, document.pk, dispatch=lambda _run: None)

    aggregates = [(model, pk) for model, pk in locks if model in (UploadBatch, Document)]
    expected_batches = [document.batch_id]
    if operation == "request":
        expected_batches.append(other_batch.pk)
    assert aggregates == [(UploadBatch, pk) for pk in sorted(expected_batches)] + [(Document, document.pk)]


@pytest.mark.parametrize("operation", ["acquire", "recover"])
def test_processing_acquisition_and_recovery_request_document_lock_before_run(monkeypatch, django_user_model, operation):
    from apps.processing.runner import PipelineResult, recover_processing_runs, run_processing

    _client, patient = _patient(django_user_model, "m")
    document, _pages = _document(patient, content_type="image/png", page_count=1, status=DocumentStatus.PROCESSING)
    run = ProcessingRun.objects.create(
        document=document, parser_version="locks", task_type="locks", idempotency_key=str(document.pk),
        heartbeat_at=timezone.now() - timedelta(minutes=20),
    )
    locks = _trace_locked_rows(monkeypatch)

    if operation == "acquire":
        run_processing(run.pk, lambda _context: PipelineResult.original_only())
    else:
        assert recover_processing_runs() == (run.pk,)

    from apps.patients.models import Patient
    assert locks[:4] == [(Patient, document.patient_id), (UploadBatch, document.batch_id), (Document, document.pk), (ProcessingRun, run.pk)]


def test_version_retention_keeps_deletion_job_until_every_version_is_erased(django_user_model):
    _client, patient = _patient(django_user_model, "n")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    client = VersionedS3Client(page_size=1)
    first = client.add_version(document.original_object_key)
    client.add_version(document.original_object_key, marker=True)
    client.failed_version_ids.add(first)
    store = S3ObjectStore(client, "private-bucket")
    job = request_document_deletion(patient, document.pk, dispatch=lambda _job_id: None)

    result = purge_document_deletion(job.pk, store)

    assert result.outcome == DeletionOutcome.RETRY_SCHEDULED
    assert DocumentDeletionJob.objects.filter(pk=job.pk).exists()
    assert Document.objects.filter(pk=document.pk, deleted_at__isnull=False).exists()
    client.failed_version_ids.clear()
    assert purge_document_deletion(job.pk, store).outcome == DeletionOutcome.PURGED
    assert client.versions == {}
    assert not DocumentDeletionJob.objects.filter(pk=job.pk).exists()


def _attach_created_item(document):
    return UploadItem.objects.create(
        batch=document.batch,
        ordinal=1,
        display_filename=document.display_filename,
        byte_size=document.byte_size,
        page_count=document.page_count,
        status=UploadItemStatus.CREATED,
        document=document,
    )


def test_document_delete_requires_confirmation_then_immediately_hides_every_entrypoint(
    django_user_model, django_capture_on_commit_callbacks
):
    client, patient = _patient(django_user_model, "p")
    other_client, _other = _patient(django_user_model, "q")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    item = _attach_created_item(document)
    path = f"/records/{document.pk}/delete/"

    confirmation = client.get(path)
    rejected = client.post(path, {})

    assert confirmation.status_code == 200
    assert "请再次确认" in confirmation.content.decode()
    assert "确认前，原件仍安全保留" in confirmation.content.decode()
    assert "保留期内可恢复" in confirmation.content.decode()
    assert '<form method="post">' in confirmation.content.decode()
    assert 'name="csrfmiddlewaretoken"' in confirmation.content.decode()
    assert 'class="button button--danger delete-confirm-button"' in confirmation.content.decode()
    assert rejected.status_code == 400
    document.refresh_from_db()
    assert document.deleted_at is None
    assert UploadItem.objects.filter(pk=item.pk).exists()
    assert other_client.get(path).status_code == 404

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(path, {"confirmation": "delete"})

    assert response.status_code == 302 and response["Location"] == "/records/?deleted=1"
    document.refresh_from_db()
    assert document.deleted_at is not None
    assert document.trash_expires_at == document.trashed_at + timedelta(days=30)
    assert not DocumentDeletionJob.objects.filter(document=document).exists()
    assert not UploadItem.objects.filter(pk=item.pk).exists()
    batch = UploadBatch.objects.get(pk=document.batch_id)
    assert batch.status == BatchStatus.COMPLETED and batch.file_count == 0

    records = client.get(response["Location"]).content.decode()
    assert "资料已移入回收站" in records
    assert document.display_filename not in records
    for hidden_path in (
        f"/records/{document.pk}/",
        f"/records/{document.pk}/viewer/",
        f"/records/{document.pk}/pages/1/image/",
        path,
    ):
        assert client.get(hidden_path).status_code == 404


def test_purge_removes_original_document_parse_feedback_and_empty_batch(django_user_model):
    client, patient = _patient(django_user_model, "r")
    document, _first_evidence, _second_evidence = _parsed_document(patient)
    _attach_created_item(document)
    client.post(f"/records/{document.pk}/feedback/")
    version_ids = tuple(document.parsing_versions.values_list("pk", flat=True))
    observation_ids = tuple(LabObservation.objects.filter(parsing_version_id__in=version_ids).values_list("pk", flat=True))
    batch_id = document.batch_id
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"synthetic-private-object"
    job = request_document_deletion(patient, document.pk, dispatch=lambda _job_id: None)

    result = purge_document_deletion(job.pk, store)

    assert result.outcome == DeletionOutcome.PURGED
    assert document.original_object_key not in store.objects
    assert not Document.objects.filter(pk=document.pk).exists()
    assert not DocumentDeletionJob.objects.filter(pk=job.pk).exists()
    assert not ParsingVersion.objects.filter(pk__in=version_ids).exists()
    assert not LabObservation.objects.filter(pk__in=observation_ids).exists()
    assert not InaccuracyFeedback.objects.filter(document_id=document.pk).exists()
    assert not UploadBatch.objects.filter(pk=batch_id).exists()


def test_storage_failure_keeps_durable_hidden_job_and_due_recovery_can_finish(django_user_model):
    _client, patient = _patient(django_user_model, "s")
    document, _pages = _document(patient, content_type="image/png", page_count=1)
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = b"synthetic-private-object"
    now = timezone.now()
    job = request_document_deletion(patient, document.pk, dispatch=lambda _job_id: None, now=now)
    store.fail_delete = True

    failed = purge_document_deletion(job.pk, store, now=now)

    assert failed.outcome == DeletionOutcome.RETRY_SCHEDULED
    assert failed.retry_delay == 60
    job.refresh_from_db()
    assert job.attempt_count == 1
    assert job.error_code == "storage_unavailable"
    assert job.next_attempt_at == now + timedelta(seconds=60)
    assert not due_document_deletions(now=now)
    assert due_document_deletions(now=now + timedelta(seconds=60)) == (job.pk,)
    document.refresh_from_db()
    assert document.deleted_at is not None

    store.fail_delete = False
    recovered = purge_document_deletion(job.pk, store, now=now + timedelta(seconds=60))

    assert recovered.outcome == DeletionOutcome.PURGED
    assert not Document.objects.filter(pk=document.pk).exists()
