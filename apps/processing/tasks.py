import logging

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .runner import ExecutionState, recover_processing_runs, run_processing


logger = logging.getLogger(__name__)


def get_processing_pipeline():
    factory_path = settings.PROCESSING_PIPELINE_FACTORY
    if not factory_path:
        raise ImproperlyConfigured("A processing pipeline factory has not been configured")
    factory = import_string(factory_path)
    return factory()


def safe_enqueue_processing(run_id, *, countdown=0):
    try:
        process_document.apply_async(args=[str(run_id)], countdown=countdown)
    except Exception:
        logger.warning(
            "Processing broker unavailable; durable queued run retained",
            extra={"run_id": str(run_id), "error_code": "processing_dispatch_unavailable"},
        )
        return False
    return True


@shared_task(
    bind=True,
    name="processing.process_document",
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=780,
    time_limit=840,
)
def process_document(self, run_id):
    try:
        pipeline = get_processing_pipeline()
    except ImproperlyConfigured:
        logger.warning("Processing pipeline is not configured", extra={"error_code": "pipeline_not_configured"})
        return {"state": "PIPELINE_NOT_CONFIGURED"}
    result = run_processing(run_id, pipeline)
    if result.state == ExecutionState.RETRY_SCHEDULED:
        safe_enqueue_processing(result.run_id, countdown=result.retry_delay)
    return result.as_payload()


@shared_task(name="processing.recover_stale_runs")
def recover_stale_runs():
    recovered = recover_processing_runs(dispatch=safe_enqueue_processing)
    return {"recovered": [str(run_id) for run_id in recovered], "count": len(recovered)}
