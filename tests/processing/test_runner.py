from datetime import timedelta
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
import pytest

from apps.documents.models import (
    BatchStatus,
    Document,
    DocumentStatus,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
    UploadItem,
    UploadItemStatus,
)
from apps.patients.models import Patient
from apps.processing.errors import NonRetryableProcessingError, RetryableProcessingError
from apps.processing.runner import (
    ExecutionState,
    PipelineResult,
    ProcessingContractError,
    recover_processing_runs,
    run_processing,
)


pytestmark = pytest.mark.django_db(transaction=True)


def make_run(django_user_model, *, marker=None):
    marker = marker or uuid.uuid4().hex * 2
    account = django_user_model.objects.create(phone_hash=marker, phone_encrypted="ciphertext")
    patient = Patient.objects.create(account=account, display_name="测试患者")
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=128)
    document = Document.objects.create(
        patient=patient,
        batch=batch,
        display_filename="private-report.png",
        content_type="image/png",
        byte_size=128,
        page_count=1,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_object_key=f"originals/{uuid.uuid4().hex}",
    )
    UploadItem.objects.create(
        batch=batch,
        ordinal=1,
        display_filename="private-report.png",
        byte_size=128,
        page_count=1,
        status=UploadItemStatus.CREATED,
        document=document,
    )
    run = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v1",
        task_type="initial",
        idempotency_key=f"{document.pk}:parser-v1:initial",
    )
    return batch, document, run


def test_success_is_published_once_and_duplicate_delivery_is_idempotent(django_user_model):
    batch, document, run = make_run(django_user_model)
    original_key = document.original_object_key
    calls = []
    now = timezone.now()

    def pipeline(context):
        calls.append(context.run_id)
        context.heartbeat(ProcessingStage.OCR)
        with transaction.atomic():
            context.assert_current()
        context.heartbeat(ProcessingStage.INDEXING)
        return PipelineResult.organized()

    first = run_processing(run.pk, pipeline, now=now)
    repeated = run_processing(run.pk, pipeline, now=now + timedelta(seconds=1))

    assert first.state == ExecutionState.SUCCEEDED
    assert repeated.state == ExecutionState.ALREADY_TERMINAL
    assert calls == [run.pk]
    run.refresh_from_db()
    document.refresh_from_db()
    batch.refresh_from_db()
    assert run.stage == ProcessingStage.SUCCEEDED
    assert run.is_current is True
    assert run.finished_at == now
    assert run.lease_token is None
    assert document.status == DocumentStatus.ORGANIZED
    assert document.original_object_key == original_key
    assert batch.status == BatchStatus.COMPLETED
    assert batch.completed_at == now


def test_no_structured_result_preserves_original_and_maps_public_state(django_user_model):
    batch, document, run = make_run(django_user_model)

    result = run_processing(run.pk, lambda _context: PipelineResult.original_only())

    assert result.state == ExecutionState.NO_STRUCTURED_RESULT
    run.refresh_from_db()
    document.refresh_from_db()
    batch.refresh_from_db()
    assert (run.stage, run.is_current) == (ProcessingStage.NO_STRUCTURED_RESULT, True)
    assert document.status == DocumentStatus.ORIGINAL_ONLY
    assert batch.status == BatchStatus.COMPLETED


def test_retryable_failures_use_exact_delays_then_terminalize(django_user_model):
    batch, document, run = make_run(django_user_model)
    started = timezone.now()
    calls = []

    def unavailable(_context):
        calls.append(True)
        raise RetryableProcessingError("ocr_provider_unavailable")

    first = run_processing(run.pk, unavailable, now=started)
    early = run_processing(run.pk, unavailable, now=started + timedelta(seconds=59))
    second_at = started + timedelta(seconds=60)
    second = run_processing(run.pk, unavailable, now=second_at)
    third_at = second_at + timedelta(seconds=300)
    third = run_processing(run.pk, unavailable, now=third_at)
    fourth_at = third_at + timedelta(seconds=1800)
    fourth = run_processing(run.pk, unavailable, now=fourth_at)

    assert first.state == ExecutionState.RETRY_SCHEDULED and first.retry_delay == 60
    assert early.state == ExecutionState.DEFERRED
    assert second.state == ExecutionState.RETRY_SCHEDULED and second.retry_delay == 300
    assert third.state == ExecutionState.RETRY_SCHEDULED and third.retry_delay == 1800
    assert fourth.state == ExecutionState.FAILED and fourth.retry_delay is None
    assert len(calls) == 4
    run.refresh_from_db()
    document.refresh_from_db()
    batch.refresh_from_db()
    assert run.retry_count == 3
    assert run.stage == ProcessingStage.FAILED
    assert run.next_retry_at is None
    assert run.finished_at == fourth_at
    assert run.error_code == "ocr_provider_unavailable"
    assert run.lease_token is None
    assert document.status == DocumentStatus.PROCESSING_FAILED
    assert batch.status == BatchStatus.COMPLETED


def test_nonretryable_failure_does_not_schedule_and_uses_safe_code(django_user_model):
    _batch, document, run = make_run(django_user_model)

    def rejected(_context):
        raise NonRetryableProcessingError("unsupported_document_content")

    result = run_processing(run.pk, rejected)

    assert result.state == ExecutionState.FAILED
    run.refresh_from_db()
    document.refresh_from_db()
    assert run.retry_count == 0
    assert run.error_code == "unsupported_document_content"
    assert document.status == DocumentStatus.PROCESSING_FAILED


def test_untyped_pipeline_exception_is_safely_retriable_and_never_persisted_or_logged(caplog, django_user_model):
    _batch, _document, run = make_run(django_user_model)

    def unsafe(_context):
        raise RuntimeError("patient-secret-ocr-content")

    with caplog.at_level("WARNING"):
        result = run_processing(run.pk, unsafe)

    run.refresh_from_db()
    assert result.state == ExecutionState.RETRY_SCHEDULED
    assert run.error_code == "unexpected_processing_error"
    assert "patient-secret-ocr-content" not in caplog.text


def test_changed_lease_fences_an_old_worker_from_publishing(django_user_model):
    _batch, document, run = make_run(django_user_model)

    def superseded(context):
        ProcessingRun.objects.filter(pk=context.run_id).update(lease_token=uuid.uuid4())
        return PipelineResult.organized()

    result = run_processing(run.pk, superseded)

    assert result.state == ExecutionState.LEASE_LOST
    run.refresh_from_db()
    document.refresh_from_db()
    assert run.stage == ProcessingStage.PREPARING
    assert run.is_current is False
    assert document.status == DocumentStatus.PROCESSING


def test_database_rejects_a_running_stage_without_a_lease(django_user_model):
    _batch, _document, run = make_run(django_user_model)

    run.stage = ProcessingStage.OCR
    with pytest.raises(IntegrityError), transaction.atomic():
        run.save(update_fields=["stage"])


def test_unhandled_pipeline_contract_violation_is_nonretryable(django_user_model):
    _batch, document, run = make_run(django_user_model)

    def backwards(context):
        context.heartbeat(ProcessingStage.OCR)
        context.heartbeat(ProcessingStage.PREPARING)

    result = run_processing(run.pk, backwards)

    assert result.state == ExecutionState.FAILED
    run.refresh_from_db()
    document.refresh_from_db()
    assert run.retry_count == 0
    assert run.error_code == "invalid_processing_contract"
    assert document.status == DocumentStatus.PROCESSING_FAILED


def test_assert_current_requires_a_transaction_bound_publish_guard(django_user_model):
    _batch, _document, run = make_run(django_user_model)
    observed = []

    def pipeline(context):
        with pytest.raises(ProcessingContractError):
            context.assert_current()
        with transaction.atomic():
            observed.append(context.assert_current().pk)
        return PipelineResult.original_only()

    run_processing(run.pk, pipeline)

    assert observed == [run.pk]


def test_recovery_requeues_only_older_than_fifteen_minutes_and_dispatches_after_commit(django_user_model):
    _batch, _document, run = make_run(django_user_model)
    now = timezone.now()
    exact_cutoff = now - timedelta(minutes=15)
    ProcessingRun.objects.filter(pk=run.pk).update(
        stage=ProcessingStage.OCR,
        heartbeat_at=exact_cutoff,
        started_at=exact_cutoff,
        lease_token=uuid.uuid4(),
    )
    dispatches = []

    assert recover_processing_runs(now=now, dispatch=dispatches.append) == ()
    ProcessingRun.objects.filter(pk=run.pk).update(heartbeat_at=exact_cutoff - timedelta(microseconds=1))
    recovered = recover_processing_runs(now=now, dispatch=dispatches.append)

    assert recovered == (run.pk,)
    assert dispatches == [run.pk]
    run.refresh_from_db()
    assert run.stage == ProcessingStage.QUEUED
    assert run.lease_token is None
    assert run.heartbeat_at == now


def test_due_retry_is_recovered_without_waiting_for_stale_cutoff(django_user_model):
    _batch, _document, run = make_run(django_user_model)
    now = timezone.now()
    ProcessingRun.objects.filter(pk=run.pk).update(
        next_retry_at=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(seconds=1),
        retry_count=1,
        error_code="temporary_ocr_failure",
    )
    dispatches = []

    recovered = recover_processing_runs(now=now, dispatch=dispatches.append)

    assert recovered == (run.pk,)
    assert dispatches == [run.pk]
    run.refresh_from_db()
    assert run.next_retry_at is None
    assert run.stage == ProcessingStage.QUEUED


def test_stale_recovery_skips_a_run_superseded_by_a_newer_current_generation(django_user_model):
    _batch, document, stale = make_run(django_user_model)
    now = timezone.now()
    ProcessingRun.objects.filter(pk=stale.pk).update(
        stage=ProcessingStage.OCR,
        heartbeat_at=now - timedelta(minutes=16),
        lease_token=uuid.uuid4(),
    )
    newer = ProcessingRun.objects.create(
        document=document,
        parser_version="parser-v2",
        task_type="reparse",
        idempotency_key=f"{document.pk}:parser-v2:reparse",
        attempt_number=2,
        stage=ProcessingStage.SUCCEEDED,
        finished_at=now,
        is_current=True,
    )
    ProcessingRun.objects.filter(pk=newer.pk).update(created_at=stale.created_at)
    newer.refresh_from_db()
    assert newer.created_at == stale.created_at
    dispatches = []

    recovered = recover_processing_runs(now=now, dispatch=dispatches.append)

    assert recovered == ()
    assert dispatches == []
    stale.refresh_from_db()
    assert stale.stage == ProcessingStage.OCR
