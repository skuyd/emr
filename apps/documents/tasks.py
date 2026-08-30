import logging

from celery import shared_task

from .backends import get_object_store
from .deletion import DeletionOutcome, due_document_deletions, purge_document_deletion


logger = logging.getLogger(__name__)


def safe_enqueue_document_deletion(job_id, *, countdown=0):
    try:
        purge_deleted_document.apply_async(args=[str(job_id)], countdown=countdown)
    except Exception:
        logger.warning(
            "Document deletion broker unavailable; durable job retained",
            extra={"job_id": str(job_id), "error_code": "deletion_dispatch_unavailable"},
        )
        return False
    return True


@shared_task(name="documents.purge_deleted_document", acks_late=True, reject_on_worker_lost=True)
def purge_deleted_document(job_id):
    result = purge_document_deletion(job_id, get_object_store())
    if result.outcome == DeletionOutcome.RETRY_SCHEDULED:
        safe_enqueue_document_deletion(job_id, countdown=result.retry_delay)
    return {"outcome": result.outcome.value, "retry_delay": result.retry_delay}


@shared_task(name="documents.recover_deletion_jobs")
def recover_deletion_jobs():
    job_ids = due_document_deletions()
    for job_id in job_ids:
        safe_enqueue_document_deletion(job_id)
    return {"count": len(job_ids)}
