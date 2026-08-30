import json

from django.conf import settings


class PushDeliveryUnavailable(RuntimeError):
    pass


class PushSubscriptionGone(PushDeliveryUnavailable):
    pass


class PyWebPushSender:
    def send(self, subscription_info, payload):
        if not settings.WEBPUSH_ENABLED:
            raise PushDeliveryUnavailable("Web Push is disabled")
        if not settings.WEBPUSH_VAPID_PRIVATE_KEY or not settings.WEBPUSH_VAPID_SUBJECT:
            raise PushDeliveryUnavailable("Web Push credentials are unavailable")
        try:
            from pywebpush import WebPushException, webpush
        except ImportError as exc:
            raise PushDeliveryUnavailable("Web Push client is unavailable") from exc
        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.WEBPUSH_VAPID_SUBJECT},
                ttl=settings.WEBPUSH_TTL_SECONDS,
                timeout=settings.WEBPUSH_TIMEOUT_SECONDS,
            )
        except WebPushException as exc:
            status_code = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status_code in {404, 410}:
                raise PushSubscriptionGone("Push subscription expired") from exc
            raise PushDeliveryUnavailable("Push provider request failed") from exc


def get_webpush_sender():
    return PyWebPushSender()
