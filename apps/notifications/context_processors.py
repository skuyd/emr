from django.conf import settings

from .models import TaskNotification, NotificationReceipt
from .services import serialize_notification


def notification_center(request):
    patient = getattr(request, "patient", None)
    if patient is None:
        return {
            "navigation_unread_notification_count": 0,
            "navigation_notifications": (),
            "webpush_public_key": "",
        }
    unread = TaskNotification.objects.filter(patient=patient).exclude(
        pk__in=NotificationReceipt.objects.filter(account=request.user, read_at__isnull=False).values("notification_id"),
    )
    return {
        "navigation_unread_notification_count": unread.count(),
        "navigation_notifications": tuple(
            serialize_notification(item, request.user) for item in unread.order_by("-created_at", "-pk")[:5]
        ),
        "webpush_public_key": settings.WEBPUSH_VAPID_PUBLIC_KEY if settings.WEBPUSH_ENABLED else "",
    }

