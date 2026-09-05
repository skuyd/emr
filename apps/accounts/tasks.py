import logging

from celery import shared_task

from apps.documents.models import DocumentDeletionJob
from apps.documents.tasks import safe_enqueue_document_deletion

from .deletion import AccountDeletionOutcome, due_account_deletions, purge_account_deletion


logger = logging.getLogger(__name__)


def safe_enqueue_account_deletion(job_id, *, countdown=0):
    try:
        purge_deleted_account.apply_async(args=[str(job_id)], countdown=countdown)
    except Exception:
        logger.warning(
            "Account deletion broker unavailable; durable job retained",
            extra={"job_id": str(job_id), "error_code": "account_deletion_dispatch_unavailable"},
        )
        return False
    return True


@shared_task(name="accounts.purge_deleted_account", acks_late=True, reject_on_worker_lost=True)
def purge_deleted_account(job_id):
    result = purge_account_deletion(job_id)
    if result.outcome == AccountDeletionOutcome.RETRY_SCHEDULED:
        for document_job_id in DocumentDeletionJob.objects.filter(
            document__patient__account__deletion_job__pk=job_id
        ).values_list("pk", flat=True):
            safe_enqueue_document_deletion(document_job_id)
        safe_enqueue_account_deletion(job_id, countdown=result.retry_delay)
    return {"outcome": result.outcome.value, "retry_delay": result.retry_delay}


@shared_task(name="accounts.recover_deletion_jobs")
def recover_account_deletion_jobs():
    job_ids = due_account_deletions()
    for job_id in job_ids:
        safe_enqueue_account_deletion(job_id)
    return {"count": len(job_ids)}


@shared_task(name="accounts.deliver_sms", acks_late=True, reject_on_worker_lost=True)
def deliver_sms(job_id):
    from .sms_delivery import deliver_sms_job

    return {"outcome": deliver_sms_job(job_id)}


@shared_task(name="accounts.recover_sms_deliveries")
def recover_sms_deliveries():
    from .sms_delivery import due_sms_deliveries

    job_ids = due_sms_deliveries()
    for job_id in job_ids:
        try:
            deliver_sms.apply_async(args=[str(job_id)])
        except Exception:
            logger.warning("SMS broker unavailable; durable delivery retained")
    return {"count": len(job_ids)}
