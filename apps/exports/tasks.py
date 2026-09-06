import logging

from celery import shared_task

from apps.documents.backends import get_object_store
from .services import generate_export, recover_exports


logger = logging.getLogger(__name__)


def safe_enqueue_export(job_id):
    try:
        generate_file.apply_async(args=[str(job_id)])
    except Exception:
        logger.warning("Export broker unavailable; durable job retained",
                       extra={"job_id": str(job_id), "error_code": "export_dispatch_unavailable"})
        return False
    return True


@shared_task(name="exports.generate_file", acks_late=True, reject_on_worker_lost=True, time_limit=1500, soft_time_limit=1440)
def generate_file(job_id):
    generate_export(job_id, get_object_store())


@shared_task(name="exports.recover_jobs")
def recover_jobs():
    return {"count": recover_exports(get_object_store(), dispatch=safe_enqueue_export)}
