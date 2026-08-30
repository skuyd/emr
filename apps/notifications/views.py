import json
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.decorators import patient_required
from apps.operations.audit import record_audit_event

from .models import TaskNotification
from .services import (
    InvalidPushSubscription,
    revoke_push_subscriptions as revoke_patient_push_subscriptions,
    serialize_notification,
    upsert_push_subscription,
)


def _private_json(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    return response


def _json_body(request):
    if len(request.body) > 16 * 1024:
        raise ValueError("Request too large")
    try:
        value = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


@patient_required
@require_GET
def notification_list(request):
    notifications = TaskNotification.objects.filter(patient=request.patient).order_by("-created_at", "-pk")[:20]
    return _private_json(
        {
            "unread_count": TaskNotification.objects.filter(
                patient=request.patient,
                read_at__isnull=True,
            ).count(),
            "notifications": [serialize_notification(item) for item in notifications],
        }
    )


@patient_required
@require_POST
def mark_notification_read(request, notification_id):
    updated = TaskNotification.objects.filter(
        pk=notification_id,
        patient=request.patient,
        read_at__isnull=True,
    ).update(read_at=timezone.now())
    if not updated and not TaskNotification.objects.filter(pk=notification_id, patient=request.patient).exists():
        raise Http404
    return _private_json({"read": True})


@patient_required
@require_GET
def open_notification(request, notification_id):
    updated = TaskNotification.objects.filter(pk=notification_id, patient=request.patient).update(
        read_at=timezone.now()
    )
    if not updated:
        raise Http404
    return redirect("/#home-tasks-title")


@patient_required
@require_POST
def create_push_subscription(request):
    try:
        payload = _json_body(request)
        if set(payload) != {"endpoint", "keys", "browser_family"} or not isinstance(payload["keys"], dict):
            raise ValueError("Invalid subscription shape")
        if set(payload["keys"]) != {"p256dh", "auth"}:
            raise ValueError("Invalid subscription keys")
        subscription = upsert_push_subscription(
            request.patient,
            payload["endpoint"],
            payload["keys"]["p256dh"],
            payload["keys"]["auth"],
            browser_family=payload["browser_family"],
        )
    except (KeyError, TypeError, ValueError, InvalidPushSubscription):
        return _private_json({"error": "invalid_subscription"}, status=400)
    record_audit_event(request.user.pk, "push_subscription_created", subscription.pk, "succeeded")
    return _private_json({"subscription_id": str(subscription.pk)}, status=201)


@patient_required
@require_POST
def revoke_push_subscription(request):
    try:
        payload = _json_body(request)
    except ValueError:
        return _private_json({"error": "invalid_request"}, status=400)
    if set(payload) not in (set(), {"endpoint"}):
        return _private_json({"error": "invalid_request"}, status=400)
    endpoint = payload.get("endpoint")
    deleted = revoke_patient_push_subscriptions(request.patient, endpoint=endpoint)
    record_audit_event(request.user.pk, "push_subscription_revoked", request.patient.pk, "succeeded")
    return _private_json({"revoked": deleted})


@require_GET
def service_worker(request):
    path = Path(settings.BASE_DIR) / "static" / "service-worker.js"
    response = FileResponse(path.open("rb"), content_type="text/javascript; charset=utf-8")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Service-Worker-Allowed"] = "/"
    response["X-Content-Type-Options"] = "nosniff"
    return response
