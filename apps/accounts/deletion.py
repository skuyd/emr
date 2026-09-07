from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from functools import partial

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.db.models import F
from django.utils import timezone

from apps.analytics.events import days_bucket, record_product_event
from apps.documents.deletion import request_document_deletion
from apps.documents.models import Document, DocumentDeletionJob
from apps.patients.models import Patient
from apps.notifications.services import revoke_push_subscriptions
from apps.operations.audit import record_audit_event
from apps.operations.models import TombstoneKind
from apps.operations.tombstones import record_deletion_tombstone

from .models import Account, AccountDeletionJob, OtpChallenge, OtpThrottle, PasswordAttemptThrottle
from .session_registry import revoke_account_sessions


RETRY_DELAYS = (60, 300, 1800, 7200, 21600)


class AccountDeletionUnavailable(ValueError):
    pass


class AccountDeletionOutcome(str, Enum):
    PURGED = "PURGED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class AccountDeletionResult:
    outcome: AccountDeletionOutcome
    retry_delay: int | None = None


def request_account_deletion(account_id, *, document_dispatch, account_dispatch, now=None):
    from apps.patients.access import invalidate_member_access
    from apps.patients.deletion import request_patient_deletion
    from apps.patients.models import PatientMembership

    now = now or timezone.now()
    with transaction.atomic():
        # Existing patient writes may create an actor FK while holding Patient.
        # NO KEY UPDATE permits their FK KEY SHARE lock, so deletion can wait
        # for Patient without forming Account -> Patient -> Account cycles.
        account = Account.objects.select_for_update(no_key=True).filter(pk=account_id, is_active=True).first()
        if account is None:
            raise AccountDeletionUnavailable()
        patient_ids = Patient.objects.filter(Q(account=account) | Q(memberships__account=account)).values("pk")
        patients = list(Patient.objects.select_for_update().filter(pk__in=patient_ids).order_by("pk"))
        for patient in patients:
            if patient.account_id == account.pk:
                if patient.deleted_at is None:
                    request_patient_deletion(patient.pk, account, document_dispatch=document_dispatch, now=now)
            else:
                memberships = PatientMembership.objects.filter(patient=patient, account=account, revoked_at__isnull=True)
                member_ids = list(memberships.values_list("pk", flat=True))
                memberships.update(revoked_at=now, revision=F("revision") + 1)
                for member_id in member_ids:
                    record_audit_event(account.pk, "member_access_revoked", member_id, "succeeded", "account_deleted", patient_id=patient.pk)
                invalidate_member_access(patient, account.pk, actor=account)
        account.is_active = False
        account.set_unusable_password()
        account.save(update_fields=["is_active", "password", "updated_at"])
        record_deletion_tombstone(TombstoneKind.ACCOUNT, account.pk, now=now)
        revoke_account_sessions(account.pk)
        job = AccountDeletionJob.objects.create(account=account)
        usage_days = max(0, (now.date() - account.date_joined.date()).days)
        record_product_event("account_deleted", {"usage_days_bucket": days_bucket(usage_days)}, account_id=account.pk)
        record_audit_event(account.pk, "account_deletion_requested", account.pk, "scheduled", "user_confirmed")
        transaction.on_commit(partial(account_dispatch, job.pk))
    return job


def _retry(job, now, code):
    delay = RETRY_DELAYS[min(job.attempt_count, len(RETRY_DELAYS) - 1)]
    job.attempt_count = min(job.attempt_count + 1, 65535)
    job.next_attempt_at = now + timedelta(seconds=delay)
    job.error_code = code
    job.save(update_fields=["attempt_count", "next_attempt_at", "error_code", "updated_at"])
    return AccountDeletionResult(AccountDeletionOutcome.RETRY_SCHEDULED, delay)


def purge_account_deletion(job_id, *, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        job = AccountDeletionJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return AccountDeletionResult(AccountDeletionOutcome.NOT_FOUND)
        patient_ids = list(Patient.objects.filter(account_id=job.account_id).values_list("pk", flat=True))
        if Document.objects.filter(patient_id__in=patient_ids).exists():
            return _retry(job, now, "document_deletion_pending")
        from apps.exports.models import ExportJob

        if ExportJob.objects.filter(patient_id__in=patient_ids, cleanup_pending=True).exists():
            return _retry(job, now, "export_cleanup_pending")

        from apps.patients.deletion import purge_patient_deletions
        purge_patient_deletions(patient_ids=patient_ids)
        if Patient.objects.filter(account_id=job.account_id).exists():
            return _retry(job, now, "patient_deletion_pending")

        account_snapshot = Account.objects.filter(pk=job.account_id).values(
            "phone_hash"
        ).first()
        if account_snapshot is None:
            return AccountDeletionResult(AccountDeletionOutcome.NOT_FOUND)
        phone_hash = account_snapshot["phone_hash"]

        # Match every same-phone authentication writer: mutex, Account, then
        # Challenges. Rows are explicitly locked before their later deletes.
        OtpThrottle.objects.get_or_create(
            scope="phone",
            identifier_hash=phone_hash,
        )
        phone_throttle = OtpThrottle.objects.select_for_update().get(
            scope="phone",
            identifier_hash=phone_hash,
        )
        account = Account.objects.select_for_update().filter(
            pk=job.account_id,
            phone_hash=phone_hash,
        ).first()
        if account is None:
            return AccountDeletionResult(AccountDeletionOutcome.NOT_FOUND)
        challenge_ids = list(
            OtpChallenge.objects.select_for_update()
            .filter(Q(phone_hash=phone_hash) | Q(account_id=account.pk))
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        revoke_account_sessions(account.pk)
        if challenge_ids:
            OtpChallenge.objects.filter(pk__in=challenge_ids).delete()
        OtpThrottle.objects.filter(pk=phone_throttle.pk).delete()
        PasswordAttemptThrottle.objects.filter(scope="phone", identifier_hash=phone_hash).delete()
        record_audit_event("system", "account_deletion_purged", account.pk, "succeeded")
        account.delete()
    try:
        cache.delete(f"otp:cooldown:{phone_hash}")
    except Exception:
        pass
    return AccountDeletionResult(AccountDeletionOutcome.PURGED)


def due_account_deletions(*, now=None, limit=100):
    now = now or timezone.now()
    return tuple(
        AccountDeletionJob.objects.filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[: max(1, min(int(limit), 1000))]
    )
