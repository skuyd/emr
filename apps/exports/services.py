"""Export locks follow patient -> documents -> daily records -> job -> attempt."""

from datetime import timedelta
from functools import partial
import hmac
import logging
import uuid

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.documents.errors import ObjectNotFound, UploadDomainError
from apps.documents.storage import _copy_verified
from apps.facts.readmodels import digest
from apps.operations.audit import record_audit_event
from apps.patients.models import Patient
from apps.patients.access import authorize_patient, owner_actor, Capability

from .content import assert_snapshot_current, build_snapshot
from .errors import ExportInputError, ExportUnavailable, PdfUnavailable, SnapshotChanged
from .formats import build_artifact, validate_options
from .files import Artifact, private_temporary_file
from .models import ExportAttempt, ExportJob, ExportSource, ExportStatus
from .sessions import session_digest, session_is_active


logger = logging.getLogger(__name__)
HIDDEN = {ExportStatus.CANCELLED, ExportStatus.INVALIDATED, ExportStatus.EXPIRED}
RETENTION = timedelta(hours=24)
LEASE = timedelta(minutes=30)


def _lock_job(job_id, *, patient=None, sources=True):
    identity = ExportJob.objects.filter(pk=job_id).values("patient_id").first()
    if identity is None or (patient is not None and identity["patient_id"] != patient.pk):
        raise PermissionDenied
    owner = Patient.objects.select_for_update().get(pk=identity["patient_id"])
    current = ExportJob.objects.get(pk=job_id)
    source_error = ""
    if sources and current.status not in HIDDEN:
        try:
            assert_snapshot_current(owner, current.snapshot)
        except (PermissionDenied, SnapshotChanged):
            source_error = "资料、核对状态或版本已变化，请重新确认。"
    job = ExportJob.objects.select_for_update().get(pk=job_id)
    job.patient = owner
    return job, source_error


def _hide(job, status, message):
    job.status = status
    job.lease_token = job.lease_expires_at = None
    job.failures = [{"code": status.lower(), "message": message}]
    job.cleanup_pending = True
    job.cleanup_retry_at = None
    # Once invalid, no derived patient content remains in this record.
    job.snapshot = {}
    job.options = {}
    job.filename = ""
    job.save()


def _validate(job, key=None, *, now=None, source_error="", actor=None):
    now = now or timezone.now()
    if actor is not None and str(getattr(actor, "pk", actor)) != str(job.requested_by_id):
        raise PermissionDenied
    if job.status in HIDDEN:
        return "此结果已取消、过期或失效，请重新选择并生成。"
    if now >= job.expires_at:
        _hide(job, ExportStatus.EXPIRED, "结果保留期已结束，请重新生成。")
        return job.failures[0]["message"]
    if key is not None and not hmac.compare_digest(session_digest(key), job.session_digest):
        return "此结果来自另一登录会话，请在当前会话重新生成。"
    try:
        access = authorize_patient(job.patient, job.requested_by_id, Capability.EXPORT)
        permitted = access.membership.revision == job.access_revision
    except PermissionDenied:
        permitted = False
    if not permitted or not session_is_active(job.patient, job.session_digest, account_id=job.requested_by_id, now=now):
        _hide(job, ExportStatus.INVALIDATED, "发起会话已失效，请重新登录并生成。")
        return job.failures[0]["message"]
    if source_error or not hmac.compare_digest(digest(job.snapshot), job.snapshot_digest):
        _hide(job, ExportStatus.INVALIDATED, source_error or "内容快照校验失败，请重新生成。")
        return job.failures[0]["message"]
    return ""


def create_preview(patient, key, selection, *, actor=None, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        access = authorize_patient(patient, owner_actor(patient, actor), Capability.EXPORT, lock=True)
        snapshot = build_snapshot(patient, selection, now=now)
        if not key or not session_is_active(patient, session_digest(key), account_id=access.actor.pk, now=now):
            raise ExportUnavailable("登录会话已失效，请重新登录。")
        job = ExportJob.objects.create(
            patient=patient, requested_by=access.actor, access_revision=access.membership.revision,
            session_digest=session_digest(key), snapshot=snapshot,
            snapshot_digest=digest(snapshot), expires_at=now + RETENTION,
        )
        # Exclusion/uncertain lists also contain source names in the frozen preview.
        # Deleting those documents must scrub their derived metadata too.
        references = {item["id"] for group in ("documents", "excluded_documents", "uncertain_documents") for item in snapshot[group]}
        ExportSource.objects.bulk_create([ExportSource(job=job, document_id=identity) for identity in references])
        from apps.self_records.models import DailyRecordExportSource
        DailyRecordExportSource.objects.bulk_create([
            DailyRecordExportSource(job=job, record_id=row['id']) for row in snapshot.get('self_records', [])
        ])
        record_audit_event(access.actor.pk, "export_preview_created", job.pk, "succeeded", patient_id=patient.pk)
    return job


def get_preview(patient, key, job_id, *, actor=None, now=None):
    with transaction.atomic():
        job, source_error = _lock_job(job_id, patient=patient)
        error = _validate(job, key, actor=owner_actor(patient, actor), now=now, source_error=source_error)
    if error:
        raise ExportUnavailable(error)
    return job


def request_generation(patient, key, job_id, options, *, dispatch, actor=None, now=None):
    with transaction.atomic():
        job, source_error = _lock_job(job_id, patient=patient)
        error = _validate(job, key, actor=owner_actor(patient, actor), now=now, source_error=source_error)
        if not error:
            options = validate_options(options, job.snapshot)
            if job.status not in {ExportStatus.PREVIEW, ExportStatus.FAILED}:
                error = "此任务已提交；请查看当前结果。"
            elif job.cleanup_pending:
                error = "上次生成的暂存文件仍在清理，请稍后重试。"
            elif job.options and job.options != options:
                error = "重试必须沿用已确认格式；修改格式请重新预览。"
            else:
                job.generation += 1
                attempt_id = uuid.uuid4()
                ExportAttempt.objects.create(
                    id=attempt_id, job=job, generation=job.generation,
                    object_key=f"originals/exports/{job.pk}/{attempt_id.hex}",
                    staging_key=f"staging/export-{attempt_id.hex}",
                )
                job.options = options
                job.status = ExportStatus.QUEUED
                job.failures = []
                job.save()
                record_audit_event(job.requested_by_id, "export_requested", job.pk, "scheduled", patient_id=job.patient_id)
                transaction.on_commit(partial(dispatch, job.pk))
    if error:
        raise ExportUnavailable(error)
    return job


def _failure(job, error):
    message = str(error) if isinstance(error, (PdfUnavailable, ExportInputError)) else "文件生成、原件读取或存储校验失败，请稍后重试。"
    code = "pdf_layout" if isinstance(error, PdfUnavailable) else "file_unavailable"
    failure = {"code": code, "message": message}
    if getattr(error, "document_id", None):
        failure["document_id"] = str(error.document_id)
    job.status = ExportStatus.FAILED
    job.failures = [failure]
    job.cleanup_pending = True
    job.cleanup_retry_at = None
    job.lease_token = job.lease_expires_at = None
    job.save()
    record_audit_event(job.requested_by_id or "system", "export_generated", job.pk, "failed", code, patient_id=job.patient_id)


def generate_export(job_id, store, *, now=None):
    with transaction.atomic():
        try:
            job, source_error = _lock_job(job_id)
        except PermissionDenied:
            return
        queued = job.status == ExportStatus.QUEUED
        error = _validate(job, now=now, source_error=source_error)
        if error and queued:
            record_audit_event(job.requested_by_id or "system", "export_generated", job.pk, "denied", "access_changed", patient_id=job.patient_id)
        if error or job.status != ExportStatus.QUEUED:
            return
        job.status = ExportStatus.GENERATING
        token = job.lease_token = uuid.uuid4()
        job.lease_expires_at = (now or timezone.now()) + LEASE
        job.save()
        snapshot, options = job.snapshot, job.options
    artifact = None
    try:
        artifact = build_artifact(snapshot, options, store)
        sha256 = artifact.sha256
        with transaction.atomic():
            job, source_error = _lock_job(job_id)
            error = _validate(job, now=now, source_error=source_error)
            if error:
                record_audit_event(job.requested_by_id or "system", "export_generated", job.pk, "denied", "access_changed", patient_id=job.patient_id)
            if error or job.status != ExportStatus.GENERATING or job.lease_token != token:
                return
            if job.lease_expires_at <= (now or timezone.now()):
                _failure(job, ExportUnavailable())
                return
            attempt = job.attempts.select_for_update().get(generation=job.generation)
            # Planned keys are already committed. Cancellation/cleanup cannot race these writes.
            staged = store.put_staging(
                artifact.stream, expected_size=artifact.byte_size, expected_sha256=sha256,
                staging_key=attempt.staging_key,
            )
            store.promote_immutable(staged, attempt.object_key)
            store.delete(attempt.staging_key)
            if _validate(job, now=now):
                _hide(job, ExportStatus.INVALIDATED, "发起会话已失效，请重新登录并生成。")
                return
            job.status = ExportStatus.READY
            job.object_key = attempt.object_key
            job.sha256, job.byte_size = sha256, artifact.byte_size
            job.filename, job.content_type = artifact.filename, artifact.content_type
            job.completed_at = now or timezone.now()
            job.expires_at = job.completed_at + RETENTION
            job.lease_token = job.lease_expires_at = None
            job.save()
            record_audit_event(job.requested_by_id or "system", "export_generated", job.pk, "succeeded", patient_id=job.patient_id)
    except Exception as exc:
        # No exception text or patient content is logged; the durable job carries a safe reason.
        logger.warning("Export generation failed", extra={"job_id": str(job_id), "error_code": "export_generation_failed"})
        with transaction.atomic():
            try:
                current, _ = _lock_job(job_id, sources=False)
            except PermissionDenied:
                return
            if current.status == ExportStatus.GENERATING and current.lease_token == token:
                _failure(current, exc)
    finally:
        if artifact is not None:
            artifact.close()


def download_export(patient, key, job_id, store, *, actor=None, now=None):
    artifact = None
    with transaction.atomic():
        job, source_error = _lock_job(job_id, patient=patient)
        error = _validate(job, key, actor=owner_actor(patient, actor), now=now, source_error=source_error)
        if not error and job.status != ExportStatus.READY:
            error = "文件尚未生成成功。"
        if not error:
            output = None
            try:
                output = private_temporary_file()
                with store.open_private(job.object_key) as stream:
                    _copy_verified(stream, output, job.byte_size, job.sha256)
                # Storage I/O may cross the exact deadline or a session revocation.
                error = _validate(job, key, now=now)
                if not error:
                    artifact = Artifact.from_stream(output, job.filename, job.content_type,
                                                    sha256=job.sha256, byte_size=job.byte_size)
            except (UploadDomainError, ExportUnavailable, OSError):
                error = "文件不可用或完整性校验失败，请重新生成。"
                _hide(job, ExportStatus.INVALIDATED, error)
            finally:
                if artifact is None and output is not None:
                    output.close()
    if error:
        raise ExportUnavailable(error)
    return artifact


def cancel_export(patient, key, job_id, *, actor=None):
    with transaction.atomic():
        job, _ = _lock_job(job_id, patient=patient, sources=False)
        authorize_patient(patient, owner_actor(patient, actor), Capability.EXPORT)
        if str(getattr(owner_actor(patient, actor), "pk", owner_actor(patient, actor))) != str(job.requested_by_id):
            raise PermissionDenied
        if not hmac.compare_digest(session_digest(key), job.session_digest):
            raise PermissionDenied
        if job.status not in HIDDEN:
            _hide(job, ExportStatus.CANCELLED, "任务已取消，暂存文件正在清理。")
            record_audit_event(job.requested_by_id, "export_cancelled", job.pk, "succeeded", patient_id=job.patient_id)
    return job


def validate_export_stream(job_id, actor, key):
    with transaction.atomic():
        job, source_error = _lock_job(job_id)
        error = _validate(job, key, actor=actor, source_error=source_error)
    if error:
        raise PermissionDenied


def invalidate_document_exports(document):
    """Called under the document lifecycle locks; bindings survive until cleanup."""
    from apps.patients.sharing import invalidate_document_shares
    invalidate_document_shares(document)
    jobs = ExportJob.objects.select_for_update().filter(source_bindings__document=document).order_by("pk")
    for job in jobs:
        if job.status not in HIDDEN:
            _hide(job, ExportStatus.INVALIDATED, "来源资料已不可用，请重新选择。")


def invalidate_patient_exports(patient):
    jobs = ExportJob.objects.select_for_update().filter(patient=patient).order_by("pk")
    for job in jobs:
        if job.status not in HIDDEN:
            _hide(job, ExportStatus.INVALIDATED, "账号已注销，导出文件正在清理。")


def invalidate_member_exports(patient, account_id):
    for job in ExportJob.objects.select_for_update().filter(patient=patient, requested_by_id=account_id).order_by("pk"):
        if job.status not in HIDDEN:
            _hide(job, ExportStatus.INVALIDATED, "成员权限已变化，请重新确认并生成。")


def cleanup_export(job_id, store, *, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        job = ExportJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return True
        if not job.cleanup_pending:
            return job.status in HIDDEN or job.status == ExportStatus.FAILED
        attempts = job.attempts.select_for_update().filter(cleaned_at__isnull=True).order_by("pk")
        for attempt in attempts:
            try:
                for key in (attempt.staging_key, attempt.object_key):
                    try:
                        store.delete(key)
                    except ObjectNotFound:
                        pass
            except UploadDomainError:
                job.cleanup_retry_at = now + timedelta(minutes=1)
                job.save(update_fields=["cleanup_retry_at", "updated_at"])
                return False
            attempt.cleaned_at = now
            attempt.save(update_fields=["cleaned_at"])
        job.cleanup_pending = False
        job.cleanup_retry_at = None
        job.object_key = job.sha256 = job.content_type = job.filename = ""
        job.byte_size = 0
        job.save()
    return True


def recover_exports(store, *, dispatch, now=None, limit=100):
    now = now or timezone.now()
    candidates = ExportJob.objects.exclude(status__in=HIDDEN).order_by("updated_at", "pk")
    ids = list(candidates.values_list("pk", flat=True)[:limit])
    for identity in ids:
        with transaction.atomic():
            try:
                job, source_error = _lock_job(identity)
            except PermissionDenied:
                continue
            if _validate(job, now=now, source_error=source_error):
                continue
            if job.status == ExportStatus.QUEUED:
                transaction.on_commit(partial(dispatch, job.pk))
            elif job.status == ExportStatus.GENERATING and job.lease_expires_at <= now:
                _failure(job, ExportUnavailable())
            # Rotate healthy previews too, so a full first page cannot starve later jobs.
            ExportJob.objects.filter(pk=job.pk).update(updated_at=now)
    pending = ExportJob.objects.filter(cleanup_pending=True).filter(
        Q(cleanup_retry_at__isnull=True) | Q(cleanup_retry_at__lte=now),
    ).order_by("updated_at", "pk").values_list("pk", flat=True)[:limit]
    for identity in list(pending):
        cleanup_export(identity, store, now=now)
    return len(ids)
