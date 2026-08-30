from django.conf import settings

from .models import TaskNotification
from .services import serialize_notification


def notification_center(request):
    patient = getattr(request, "patient", None)
    if patient is None:
        return {
            "navigation_unread_notification_count": 0,
            "navigation_notifications": (),
            "webpush_public_key": "",
        }
    unread = TaskNotification.objects.filter(patient=patient, read_at__isnull=True)
    return {
        "navigation_unread_notification_count": unread.count(),
        "navigation_notifications": tuple(
            serialize_notification(item) for item in unread.order_by("-created_at", "-pk")[:5]
        ),
        "webpush_public_key": settings.WEBPUSH_VAPID_PUBLIC_KEY if settings.WEBPUSH_ENABLED else "",
    }

