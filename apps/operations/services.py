from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path
import re

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.core.exceptions import PermissionDenied

from apps.analytics.events import count_bucket
from apps.documents.batches import refresh_batch_state
from apps.documents.models import (
    Document,
    DocumentDeletionJob,
    DocumentStatus,
    PatientUploadQuota,
    ProcessingRun,
    ProcessingStage,
    UploadBatch,
)
from apps.labs.dictionary import load_dictionary
from apps.patients.models import Patient
from apps.processing.models import ParsingVersion

from .audit import record_audit_event
from .models import DictionaryRelease, SupportAccessGrant
from .permissions import Action, Role, authorize


_REASON = re.compile(r"[a-z][a-z0-9_]{2,63}")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,119}\.json")


class InvalidOperation(ValueError):
    pass


@dataclass(frozen=True)
class RequeueResult:
    run_id: object
    attempt_number: int
    document_id: None = None


@dataclass(frozen=True)
class QuotaLimits:
    batch_file_limit: int
    batch_page_limit: int
    document_limit: int
    page_limit: int
    storage_byte_limit: int


@dataclass(frozen=True)
class DeletionStatus:
    kind: str
    state: str
    attempt_count: int
    retry_scheduled: bool


def _reason(value):
    if not isinstance(value, str) or _REASON.fullmatch(value) is None:
        raise InvalidOperation("A stable non-sensitive reason code is required")
    return value


def requeue_processing(operator, failed_run_id, *, reason_code, dispatch):
    authorize(operator, Action.REQUEUE_PROCESSING)
    reason_code = _reason(reason_code)
    with transaction.atomic():
        failed = (
            ProcessingRun.objects.select_for_update()
            .select_related("document")
            .filter(pk=failed_run_id, stage=ProcessingStage.FAILED, document__deleted_at__isnull=True)
            .first()
        )
        if failed is None or failed.document.status != DocumentStatus.PROCESSING_FAILED:
            raise InvalidOperation("Processing run is not eligible for requeue")
        if ProcessingRun.objects.filter(
            document=failed.document,
            stage__in=(
                ProcessingStage.QUEUED,
                ProcessingStage.PREPARING,
                ProcessingStage.OCR,
                ProcessingStage.CLASSIFYING,
                ProcessingStage.EXTRACTING,
                ProcessingStage.INDEXING,
            ),
        ).exists():
            raise InvalidOperation("A processing run is already active")
        attempt = ProcessingRun.objects.filter(document=failed.document).aggregate(
            value=Max("attempt_number")
        )["value"] or 0
        attempt += 1
        try:
            run = ProcessingRun.objects.create(
                document=failed.document,
                parser_version=failed.parser_version,
                task_type=f"OPERATOR_RETRY_{attempt}",
                idempotency_key=f"{failed.document_id}:{failed.parser_version}:OPERATOR_RETRY_{attempt}",
                attempt_number=attempt,
                stage=ProcessingStage.QUEUED,
            )
        except IntegrityError:
            raise InvalidOperation("Processing requeue conflicted") from None
        failed.document.status = DocumentStatus.PROCESSING
        failed.document.save(update_fields=["status", "updated_at"])
        batch = UploadBatch.objects.select_for_update().get(pk=failed.document.batch_id)
        refresh_batch_state(batch)
        record_audit_event(operator.pk, "processing_requeued", failed.document_id, "scheduled", reason_code)
        transaction.on_commit(partial(dispatch, run.pk))
    return RequeueResult(run.pk, attempt)


def activate_parsing_version(operator, version_id, *, reason_code, totp_verified_at):
    authorize(
        operator,
        Action.ACTIVATE_PARSING_VERSION,
        totp_verified_at=totp_verified_at,
    )
    reason_code = _reason(reason_code)
    with transaction.atomic():
        version = (
            ParsingVersion.objects.select_for_update()
            .select_related("document", "processing_run")
            .get(pk=version_id)
        )
        version = ParsingVersion.objects.activate(version)
        ProcessingRun.objects.filter(document=version.document, is_current=True).exclude(
            pk=version.processing_run_id
        ).update(is_current=False)
        run = ProcessingRun.objects.select_for_update().get(pk=version.processing_run_id)
        if run.stage not in {ProcessingStage.SUCCEEDED, ProcessingStage.NO_STRUCTURED_RESULT} or run.finished_at is None:
            raise InvalidOperation("Parsing version is not terminal")
        run.is_current = True
        run.save(update_fields=["is_current", "updated_at"])
        version.document.status = (
            DocumentStatus.ORGANIZED
            if run.stage == ProcessingStage.SUCCEEDED
            else DocumentStatus.ORIGINAL_ONLY
        )
        version.document.save(update_fields=["status", "updated_at"])
        record_audit_event(operator.pk, "parsing_version_activated", version.pk, "succeeded", reason_code)
    return version


def publish_dictionary(operator, artifact_name, *, reason_code, totp_verified_at):
    authorize(operator, Action.PUBLISH_DICTIONARY, totp_verified_at=totp_verified_at)
    reason_code = _reason(reason_code)
    if not isinstance(artifact_name, str) or _ARTIFACT.fullmatch(artifact_name) is None:
        raise InvalidOperation("Invalid dictionary artifact")
    dictionary_root = Path(__file__).resolve().parent.parent / "labs" / "dictionaries"
    artifact = (dictionary_root / artifact_name).resolve()
    try:
        artifact.relative_to(dictionary_root.resolve())
    except ValueError:
        raise InvalidOperation("Invalid dictionary artifact") from None
    dictionary = load_dictionary(artifact)
    now = timezone.now()
    with transaction.atomic():
        list(DictionaryRelease.objects.select_for_update().filter(active=True))
        release = DictionaryRelease.objects.filter(version=dictionary.version).first()
        if release is not None and (
            release.content_hash != dictionary.content_hash or release.artifact_name != artifact_name
        ):
            raise InvalidOperation("Dictionary version identity is immutable")
        if release is None:
            release = DictionaryRelease.objects.create(
                version=dictionary.version,
                content_hash=dictionary.content_hash,
                artifact_name=artifact_name,
                indicator_count=len(dictionary.indicators),
                active=False,
                published_at=now,
            )
        DictionaryRelease.objects.filter(active=True).exclude(pk=release.pk).update(active=False)
        if not release.active:
            release.active = True
            release.published_at = now
            release.save(update_fields=["active", "published_at"])
        record_audit_event(operator.pk, "dictionary_published", release.pk, "succeeded", reason_code)
    return release


def _validate_limits(limits):
    if not isinstance(limits, QuotaLimits):
        raise InvalidOperation("Quota limits are required")
    values = (
        limits.batch_file_limit,
        limits.batch_page_limit,
        limits.document_limit,
        limits.page_limit,
        limits.storage_byte_limit,
    )
    if any(type(value) is not int or value <= 0 for value in values):
        raise InvalidOperation("Quota limits must be positive integers")
    if (
        limits.batch_file_limit > 20
        or limits.batch_page_limit > 60
        or limits.document_limit > 10_000
        or limits.page_limit > 100_000
        or limits.storage_byte_limit > 100 * 1024**3
    ):
        raise InvalidOperation("Quota limit exceeds the operational cap")
    return limits


def change_patient_quota(
    operator,
    patient_id,
    limits,
    *,
    reason_code,
    totp_verified_at,
):
    authorize(operator, Action.CHANGE_QUOTA, totp_verified_at=totp_verified_at)
    reason_code = _reason(reason_code)
    limits = _validate_limits(limits)
    with transaction.atomic():
        patient = Patient.objects.select_for_update().get(pk=patient_id, account__is_active=True)
        quota, _created = PatientUploadQuota.objects.get_or_create(patient=patient)
        quota = PatientUploadQuota.objects.select_for_update().get(pk=quota.pk)
        for field in QuotaLimits.__dataclass_fields__:
            setattr(quota, field, getattr(limits, field))
        quota.save(update_fields=[*QuotaLimits.__dataclass_fields__, "updated_at"])
        record_audit_event(operator.pk, "quota_changed", patient.pk, "succeeded", reason_code)
    return quota


def grant_support_access(
    operator,
    support_operator_id,
    patient_id,
    *,
    duration,
    reason_code,
    totp_verified_at,
    now=None,
):
    authorize(operator, Action.GRANT_SUPPORT_ACCESS, totp_verified_at=totp_verified_at, now=now)
    reason_code = _reason(reason_code)
    now = now or timezone.now()
    if not isinstance(duration, timedelta) or not timedelta(minutes=5) <= duration <= timedelta(hours=1):
        raise InvalidOperation("Support access duration must be between five and sixty minutes")
    from apps.accounts.models import Account

    support = Account.objects.filter(pk=support_operator_id, is_active=True, is_staff=True).first()
    if support is None or not support.groups.filter(name=Role.SUPPORT.value).exists():
        raise PermissionDenied("Operation is not permitted")
    patient = Patient.objects.get(pk=patient_id, account__is_active=True)
    with transaction.atomic():
        grant = SupportAccessGrant.objects.create(
            operator=support,
            patient=patient,
            reason_code=reason_code,
            expires_at=now + duration,
        )
        record_audit_event(operator.pk, "support_access_granted", grant.pk, "succeeded", reason_code)
    return grant


def support_metadata_summary(operator, patient_id, *, now=None):
    authorize(operator, Action.VIEW_SUPPORT_METADATA)
    now = now or timezone.now()
    grant = (
        SupportAccessGrant.objects.filter(
            operator=operator,
            patient_id=patient_id,
            revoked_at__isnull=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
    if grant is None:
        raise PermissionDenied("An active support grant is required")
    patient = Patient.objects.select_related("account").get(pk=patient_id)
    active_documents = Document.objects.filter(patient=patient, deleted_at__isnull=True).count()
    record_audit_event(operator.pk, "support_access_used", grant.pk, "succeeded", "metadata_only")
    return {
        "account_state": "active" if patient.account.is_active else "deleting",
        "document_count_bucket": count_bucket(active_documents),
        "deletion_state": "requested" if hasattr(patient.account, "deletion_job") else "none",
    }


def deletion_status(operator, kind, job_id):
    authorize(operator, Action.VIEW_DELETION_STATUS)
    if kind == "document":
        job = DocumentDeletionJob.objects.filter(pk=job_id).first()
    elif kind == "account":
        from apps.accounts.models import AccountDeletionJob

        job = AccountDeletionJob.objects.filter(pk=job_id).first()
    else:
        raise InvalidOperation("Unknown deletion job kind")
    if job is None:
        raise InvalidOperation("Deletion job is unavailable")
    record_audit_event(operator.pk, "deletion_status_viewed", job.pk, "succeeded", "status_only")
    return DeletionStatus(
        kind=kind,
        state="retry" if job.next_attempt_at is not None else "pending",
        attempt_count=job.attempt_count,
        retry_scheduled=job.next_attempt_at is not None,
    )
