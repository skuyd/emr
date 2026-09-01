from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from functools import partial

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
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
    now = now or timezone.now()
    with transaction.atomic():
        account = Account.objects.select_for_update().filter(pk=account_id, is_active=True).first()
        if account is None:
            raise AccountDeletionUnavailable()
        patient = Patient.objects.select_for_update().filter(account=account).first()
        if patient is None:
            raise AccountDeletionUnavailable()

        active_document_ids = tuple(
            Document.objects.filter(patient=patient, deleted_at__isnull=True).values_list("pk", flat=True)
        )
        previously_deleted_ids = tuple(
            Document.objects.filter(patient=patient, deleted_at__isnull=False).values_list("pk", flat=True)
        )
        for document_id in active_document_ids:
            request_document_deletion(
                patient,
                document_id,
                dispatch=document_dispatch,
                now=now,
            )
        for document in Document.objects.filter(pk__in=previously_deleted_ids):
            deletion_job, created = DocumentDeletionJob.objects.get_or_create(
                document=document,
                defaults={"object_key": document.original_object_key},
            )
            if created or deletion_job.next_attempt_at is None or deletion_job.next_attempt_at <= now:
                transaction.on_commit(partial(document_dispatch, deletion_job.pk))

        account.is_active = False
        account.set_unusable_password()
        account.save(update_fields=["is_active", "password", "updated_at"])
        record_deletion_tombstone(TombstoneKind.ACCOUNT, account.pk, now=now)
        revoke_push_subscriptions(patient)
        revoke_account_sessions(account.pk)
        job = AccountDeletionJob.objects.create(account=account)
        usage_days = max(0, (now.date() - account.date_joined.date()).days)
        record_product_event(
            "account_deleted",
            {"usage_days_bucket": days_bucket(usage_days)},
            account_id=account.pk,
        )
        record_audit_event(
            account.pk,
            "account_deletion_requested",
            account.pk,
            "scheduled",
            "user_confirmed",
        )
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
        patient_id = Patient.objects.filter(account_id=job.account_id).values_list("pk", flat=True).first()
        if patient_id is not None and Document.objects.filter(patient_id=patient_id).exists():
            return _retry(job, now, "document_deletion_pending")

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
