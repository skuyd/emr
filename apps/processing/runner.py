from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
import logging
from functools import partial
import uuid

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.documents.batches import refresh_batch_state
from apps.documents.models import (
    Document,
    DocumentStatus,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
)

from .errors import (
    NonRetryableProcessingError,
    ProcessingContractError,
    ProcessingLeaseLost,
    RetryableProcessingError,
)


logger = logging.getLogger(__name__)

RETRY_DELAYS = (60, 300, 1800)
STALE_AFTER = timedelta(minutes=15)
RUNNING_STAGES = (
    ProcessingStage.PREPARING,
    ProcessingStage.OCR,
    ProcessingStage.CLASSIFYING,
    ProcessingStage.EXTRACTING,
    ProcessingStage.INDEXING,
)
TERMINAL_STAGES = (
    ProcessingStage.SUCCEEDED,
    ProcessingStage.NO_STRUCTURED_RESULT,
    ProcessingStage.FAILED,
)
_STAGE_ORDER = {stage: index for index, stage in enumerate(RUNNING_STAGES)}


class PipelineOutcome(str, Enum):
    ORGANIZED = "ORGANIZED"
    ORIGINAL_ONLY = "ORIGINAL_ONLY"


@dataclass(frozen=True)
class PipelineResult:
    outcome: PipelineOutcome

    @classmethod
    def organized(cls):
        return cls(PipelineOutcome.ORGANIZED)

    @classmethod
    def original_only(cls):
        return cls(PipelineOutcome.ORIGINAL_ONLY)


class ExecutionState(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    NO_STRUCTURED_RESULT = "NO_STRUCTURED_RESULT"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    LEASE_LOST = "LEASE_LOST"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class ExecutionResult:
    state: ExecutionState
    run_id: uuid.UUID | str
    retry_delay: int | None = None

    def as_payload(self):
        payload = {"state": self.state.value, "run_id": str(self.run_id)}
        if self.retry_delay is not None:
            payload["retry_delay"] = self.retry_delay
        return payload


def _aware_now(value=None):
    value = timezone.now() if value is None else value
    if timezone.is_naive(value):
        raise ValueError("Processing timestamps must be timezone-aware")
    return value


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _newer_generation_exists(run):
    return ProcessingRun.objects.filter(
        document_id=run.document_id,
        attempt_number__gt=run.attempt_number,
    ).exists()


def _assert_lease(run, document, lease_token, idempotency_key, attempt_number):
    if (
        run.lease_token != lease_token
        or run.idempotency_key != idempotency_key
        or run.attempt_number != attempt_number
        or run.stage not in RUNNING_STAGES
        or run.finished_at is not None
        or run.is_current
        or document.deleted_at is not None
        or _newer_generation_exists(run)
    ):
        raise ProcessingLeaseLost("The processing lease is no longer current")


@dataclass(frozen=True)
class ProcessingContext:
    run_id: uuid.UUID
    document_id: uuid.UUID
    parser_version: str
    task_type: str
    idempotency_key: str
    attempt_number: int
    lease_token: uuid.UUID
    _clock: object

    def heartbeat(self, stage):
        try:
            target = ProcessingStage(stage)
        except (TypeError, ValueError) as error:
            raise ProcessingContractError("Unknown processing heartbeat stage") from error
        if target not in RUNNING_STAGES:
            raise ProcessingContractError("Heartbeat stage must be nonterminal")
        with transaction.atomic():
            run = ProcessingRun.objects.select_for_update().get(pk=self.run_id)
            document = Document.objects.get(pk=self.document_id)
            _assert_lease(run, document, self.lease_token, self.idempotency_key, self.attempt_number)
            current = ProcessingStage(run.stage)
            if _STAGE_ORDER[target] < _STAGE_ORDER[current]:
                raise ProcessingContractError("Processing stages cannot move backwards")
            run.stage = target
            run.heartbeat_at = self._clock()
            run.save(update_fields=["stage", "heartbeat_at", "updated_at"])

    def assert_current(self):
        """Lock and verify this lease inside the caller's publication transaction."""

        if not connection.in_atomic_block:
            raise ProcessingContractError("assert_current must run inside transaction.atomic()")
        document = Document.objects.select_for_update().get(pk=self.document_id)
        run = ProcessingRun.objects.select_for_update().get(pk=self.run_id)
        _assert_lease(run, document, self.lease_token, self.idempotency_key, self.attempt_number)
        return run


def _acquire(run_id, clock):
    with transaction.atomic():
        run = ProcessingRun.objects.select_for_update().select_related("document").filter(pk=run_id).first()
        if run is None:
            return ExecutionResult(ExecutionState.NOT_FOUND, run_id)
        if run.stage in TERMINAL_STAGES:
            return ExecutionResult(ExecutionState.ALREADY_TERMINAL, run.pk)
        if run.stage != ProcessingStage.QUEUED:
            return ExecutionResult(ExecutionState.ALREADY_RUNNING, run.pk)
        now = clock()
        if run.next_retry_at is not None and run.next_retry_at > now:
            return ExecutionResult(ExecutionState.DEFERRED, run.pk)
        if run.document.deleted_at is not None or _newer_generation_exists(run):
            return ExecutionResult(ExecutionState.LEASE_LOST, run.pk)
        lease_token = uuid.uuid4()
        run.stage = ProcessingStage.PREPARING
        run.lease_token = lease_token
        run.heartbeat_at = now
        run.next_retry_at = None
        run.error_code = ""
        if run.started_at is None:
            run.started_at = now
        run.save(
            update_fields=[
                "stage",
                "lease_token",
                "heartbeat_at",
                "next_retry_at",
                "error_code",
                "started_at",
                "updated_at",
            ]
        )
        return ProcessingContext(
            run_id=run.pk,
            document_id=run.document_id,
            parser_version=run.parser_version,
            task_type=run.task_type,
            idempotency_key=run.idempotency_key,
            attempt_number=run.attempt_number,
            lease_token=lease_token,
            _clock=clock,
        )


def _locked_aggregate(context):
    identity = ProcessingRun.objects.filter(pk=context.run_id).values("document_id", "document__batch_id").first()
    if identity is None:
        raise ProcessingLeaseLost("Processing run no longer exists")
    batch = UploadBatch.objects.select_for_update().get(pk=identity["document__batch_id"])
    document = Document.objects.select_for_update().get(pk=identity["document_id"])
    run = ProcessingRun.objects.select_for_update().get(pk=context.run_id)
    _assert_lease(
        run,
        document,
        context.lease_token,
        context.idempotency_key,
        context.attempt_number,
    )
    return batch, document, run


def _publish(context, result, finished_at):
    if not isinstance(result, PipelineResult):
        raise NonRetryableProcessingError("invalid_pipeline_result")
    if result.outcome == PipelineOutcome.ORGANIZED:
        run_stage = ProcessingStage.SUCCEEDED
        document_status = DocumentStatus.ORGANIZED
        execution_state = ExecutionState.SUCCEEDED
    elif result.outcome == PipelineOutcome.ORIGINAL_ONLY:
        run_stage = ProcessingStage.NO_STRUCTURED_RESULT
        document_status = DocumentStatus.ORIGINAL_ONLY
        execution_state = ExecutionState.NO_STRUCTURED_RESULT
    else:
        raise NonRetryableProcessingError("invalid_pipeline_result")

    with transaction.atomic():
        batch, document, run = _locked_aggregate(context)
        from apps.processing.models import ParsingVersion, ParsingVersionStatus

        parsing_version = ParsingVersion.objects.select_for_update().filter(processing_run=run).first()
        if parsing_version is not None:
            if parsing_version.status != ParsingVersionStatus.READY or parsing_version.active:
                raise NonRetryableProcessingError("invalid_pipeline_result")
            try:
                ParsingVersion.objects.activate(parsing_version, published_at=finished_at)
            except ValueError:
                raise NonRetryableProcessingError("invalid_pipeline_result") from None
        ProcessingRun.objects.filter(document=document, is_current=True).exclude(pk=run.pk).update(is_current=False)
        run.stage = run_stage
        run.finished_at = finished_at
        run.heartbeat_at = finished_at
        run.next_retry_at = None
        run.error_code = ""
        run.lease_token = None
        run.is_current = True
        run.save(
            update_fields=[
                "stage",
                "finished_at",
                "heartbeat_at",
                "next_retry_at",
                "error_code",
                "lease_token",
                "is_current",
                "updated_at",
            ]
        )
        document.status = document_status
        document.save(update_fields=["status", "updated_at"])
        refresh_batch_state(batch, now=finished_at)
    return ExecutionResult(execution_state, context.run_id)


def _terminal_failure(context, code, finished_at):
    try:
        with transaction.atomic():
            batch, document, run = _locked_aggregate(context)
            run.stage = ProcessingStage.FAILED
            run.finished_at = finished_at
            run.heartbeat_at = finished_at
            run.next_retry_at = None
            run.error_code = code
            run.lease_token = None
            run.is_current = False
            run.save(
                update_fields=[
                    "stage",
                    "finished_at",
                    "heartbeat_at",
                    "next_retry_at",
                    "error_code",
                    "lease_token",
                    "is_current",
                    "updated_at",
                ]
            )
            document.status = DocumentStatus.PROCESSING_FAILED
            document.save(update_fields=["status", "updated_at"])
            refresh_batch_state(batch, now=finished_at)
    except ProcessingLeaseLost:
        return ExecutionResult(ExecutionState.LEASE_LOST, context.run_id)
    return ExecutionResult(ExecutionState.FAILED, context.run_id)


def _retry_or_fail(context, code, failed_at):
    snapshot = ProcessingRun.objects.filter(pk=context.run_id).values_list("retry_count", flat=True).first()
    if snapshot is None:
        return ExecutionResult(ExecutionState.LEASE_LOST, context.run_id)
    if snapshot >= len(RETRY_DELAYS):
        return _terminal_failure(context, code, failed_at)
    try:
        with transaction.atomic():
            run = ProcessingRun.objects.select_for_update().get(pk=context.run_id)
            document = Document.objects.get(pk=context.document_id)
            _assert_lease(
                run,
                document,
                context.lease_token,
                context.idempotency_key,
                context.attempt_number,
            )
            if run.retry_count >= len(RETRY_DELAYS):
                raise ProcessingLeaseLost("Retry state changed while handling a failure")
            delay = RETRY_DELAYS[run.retry_count]
            run.retry_count += 1
            run.stage = ProcessingStage.QUEUED
            run.next_retry_at = failed_at + timedelta(seconds=delay)
            run.heartbeat_at = failed_at
            run.error_code = code
            run.lease_token = None
            run.save(
                update_fields=[
                    "retry_count",
                    "stage",
                    "next_retry_at",
                    "heartbeat_at",
                    "error_code",
                    "lease_token",
                    "updated_at",
                ]
            )
    except ProcessingLeaseLost:
        return ExecutionResult(ExecutionState.LEASE_LOST, context.run_id)
    return ExecutionResult(ExecutionState.RETRY_SCHEDULED, context.run_id, retry_delay=delay)


def _invoke_pipeline(pipeline, context):
    runner = getattr(pipeline, "run", None)
    if callable(runner):
        return runner(context)
    if callable(pipeline):
        return pipeline(context)
    raise NonRetryableProcessingError("invalid_pipeline_contract")


def run_processing(run_id, pipeline, now=None):
    """Acquire one fenced lease, run outside a transaction, then publish atomically."""

    normalized_id = _uuid_or_none(run_id)
    if normalized_id is None:
        return ExecutionResult(ExecutionState.NOT_FOUND, str(run_id))
    fixed_now = _aware_now(now) if now is not None else None
    clock = (lambda: fixed_now) if fixed_now is not None else timezone.now
    acquired = _acquire(normalized_id, clock)
    if isinstance(acquired, ExecutionResult):
        return acquired
    context = acquired
    try:
        result = _invoke_pipeline(pipeline, context)
        return _publish(context, result, _aware_now(clock()))
    except ProcessingLeaseLost:
        return ExecutionResult(ExecutionState.LEASE_LOST, context.run_id)
    except RetryableProcessingError as error:
        return _retry_or_fail(context, error.code, _aware_now(clock()))
    except NonRetryableProcessingError as error:
        return _terminal_failure(context, error.code, _aware_now(clock()))
    except Exception:
        logger.warning(
            "Processing pipeline raised an untyped exception",
            extra={"run_id": str(context.run_id), "error_code": "unexpected_processing_error"},
        )
        return _retry_or_fail(context, "unexpected_processing_error", _aware_now(clock()))


def _eligible_for_recovery(run, now, cutoff):
    if run.stage == ProcessingStage.QUEUED:
        if run.next_retry_at is not None:
            return run.next_retry_at <= now
        reference = run.heartbeat_at or run.created_at
        return reference < cutoff
    return run.stage in RUNNING_STAGES and run.heartbeat_at is not None and run.heartbeat_at < cutoff


def _safe_dispatch(dispatch, run_id):
    try:
        dispatch(run_id)
    except Exception:
        logger.warning(
            "Processing dispatch failed; durable queued run retained",
            extra={"run_id": str(run_id), "error_code": "processing_dispatch_unavailable"},
        )


def recover_processing_runs(*, now=None, dispatch=None, limit=100):
    """Fence stale workers and re-dispatch due durable work."""

    now = _aware_now(now)
    cutoff = now - STALE_AFTER
    candidate_ids = list(
        ProcessingRun.objects.filter(stage__in=(ProcessingStage.QUEUED,) + RUNNING_STAGES)
        .filter(
            Q(stage=ProcessingStage.QUEUED, next_retry_at__lte=now)
            | Q(stage=ProcessingStage.QUEUED, next_retry_at__isnull=True, heartbeat_at__lt=cutoff)
            | Q(stage=ProcessingStage.QUEUED, next_retry_at__isnull=True, heartbeat_at__isnull=True, created_at__lt=cutoff)
            | Q(stage__in=RUNNING_STAGES, heartbeat_at__lt=cutoff)
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[: max(1, min(int(limit), 1000))]
    )
    recovered = []
    for run_id in candidate_ids:
        with transaction.atomic():
            run = ProcessingRun.objects.select_for_update().select_related("document").get(pk=run_id)
            if not _eligible_for_recovery(run, now, cutoff):
                continue
            if run.document.deleted_at is not None or _newer_generation_exists(run):
                continue
            run.stage = ProcessingStage.QUEUED
            run.lease_token = None
            run.heartbeat_at = now
            run.next_retry_at = None
            run.save(update_fields=["stage", "lease_token", "heartbeat_at", "next_retry_at", "updated_at"])
            recovered.append(run.pk)
            if dispatch is not None:
                transaction.on_commit(partial(_safe_dispatch, dispatch, run.pk))
    return tuple(recovered)
