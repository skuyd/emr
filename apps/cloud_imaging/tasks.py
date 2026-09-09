import logging

from celery import shared_task

from apps.documents.backends import get_object_store
from .scan_services import recover_scans, run_scan


logger = logging.getLogger(__name__)


def safe_enqueue_scan(scan_id):
    try:
        scan_document.apply_async(args=[str(scan_id)])
    except Exception:
        logger.warning('Cloud scan broker unavailable; durable job retained',
                       extra={'scan_id': str(scan_id), 'error_code': 'cloud_scan_dispatch_unavailable'})
        return False
    return True


@shared_task(name='cloud_imaging.scan_document', acks_late=True, reject_on_worker_lost=True, time_limit=1200, soft_time_limit=1140)
def scan_document(scan_id):
    run_scan(scan_id, get_object_store())


@shared_task(name='cloud_imaging.recover_scans')
def recover_jobs():
    return {'count': recover_scans(dispatch=safe_enqueue_scan)}
