import logging

from celery import shared_task

from .services import PushDeliveryStatus, deliver_push, due_push_deliveries


logger = logging.getLogger(__name__)


def safe_enqueue_push_delivery(delivery_id, *, countdown=0):
    try:
        send_push_notification.apply_async(args=[str(delivery_id)], countdown=countdown)
    except Exception:
        logger.warning(
            "Push delivery broker unavailable; durable job retained",
            extra={"delivery_id": str(delivery_id), "error_code": "push_dispatch_unavailable"},
        )
        return False
    return True


@shared_task(name="notifications.send_push", acks_late=True, reject_on_worker_lost=True)
def send_push_notification(delivery_id):
    result = deliver_push(delivery_id)
    if result.status == PushDeliveryStatus.RETRY:
        safe_enqueue_push_delivery(delivery_id, countdown=result.retry_delay)
    return {"status": result.status, "retry_delay": result.retry_delay}


@shared_task(name="notifications.recover_push_deliveries")
def recover_push_deliveries():
    delivery_ids = due_push_deliveries()
    for delivery_id in delivery_ids:
        safe_enqueue_push_delivery(delivery_id)
    return {"count": len(delivery_ids)}

