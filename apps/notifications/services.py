from dataclasses import dataclass
from datetime import timedelta
from functools import partial
import logging
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.documents.batches import summarize_batch
from apps.documents.models import BatchStatus, UploadBatch
from apps.patients.models import Patient, PatientPreference
from apps.operations.metrics import safe_record_metric

from .crypto import (
    decrypt_subscription_value,
    encrypt_subscription_value,
    endpoint_hash,
)
from .models import (
    NotificationKind,
    PushDelivery,
    PushDeliveryStatus,
    PushSubscription,
    TaskNotification,
)
from .webpush import PushDeliveryUnavailable, PushSubscriptionGone, get_webpush_sender


logger = logging.getLogger(__name__)

NOTIFICATION_COPY = {
    NotificationKind.COMPLETED: (
        "资料整理完成",
        "你上传的资料已整理完成，点击查看结果。",
    ),
    NotificationKind.FAILED: (
        "资料整理有未完成项目",
        "你上传的资料中有未完成项目，点击查看任务状态。",
    ),
}
RETRY_DELAYS = (60, 300, 1800, 7200, 21600)
_KEY_VALUE = re.compile(r"[A-Za-z0-9_-]{16,512}")
_BROWSERS = {choice[0] for choice in PushSubscription.BROWSER_CHOICES}


class InvalidPushSubscription(ValueError):
    pass


@dataclass(frozen=True)
class PushDeliveryResult:
    status: str
    retry_delay: int | None = None


def notification_copy(kind):
    try:
        return NOTIFICATION_COPY[kind]
    except KeyError as exc:
        raise ValueError("Unknown task notification kind") from exc


def serialize_notification(notification):
    title, body = notification_copy(notification.kind)
    return {
        "notification_id": str(notification.pk),
        "title": title,
        "body": body,
        "created_at": notification.created_at.isoformat(),
        "read": notification.read_at is not None,
    }


def serialize_push_notification(notification):
    title, body = notification_copy(notification.kind)
    return {
        "notification_id": str(notification.pk),
        "title": title,
        "body": body,
    }


def _lock_notification_patient(patient_id, account_id):
    # Match account deletion before acquiring batch, subscription or delivery
    # locks. Joined FOR UPDATE queries otherwise lock child rows first.
    account = Account.objects.select_for_update().filter(pk=account_id).first()
    if account is None:
        return None
    patient = Patient.objects.select_for_update().filter(pk=patient_id, account=account).first()
    if patient is not None:
        patient.account = account
    return patient


def create_task_notification(batch_id, *, dispatch=None):
    if dispatch is None:
        from .tasks import safe_enqueue_push_delivery

        dispatch = safe_enqueue_push_delivery
    with transaction.atomic():
        identity = UploadBatch.objects.filter(pk=batch_id).values("patient_id", "patient__account_id").first()
        if identity is None:
            return None
        patient = _lock_notification_patient(identity["patient_id"], identity["patient__account_id"])
        if patient is None or not patient.account.is_active:
            return None
        batch = (
            UploadBatch.objects.select_for_update()
            .filter(pk=batch_id, patient=patient)
            .first()
        )
        if batch is None or batch.status != BatchStatus.COMPLETED:
            return None
        batch.patient = patient
        counts = summarize_batch(batch)
        if not counts.terminal:
            return None
        kind = NotificationKind.FAILED if counts.failed else NotificationKind.COMPLETED
        notification, created = TaskNotification.objects.get_or_create(
            batch=batch,
            defaults={"patient": batch.patient, "kind": kind},
        )
        if not created:
            return notification
        enabled = PatientPreference.objects.filter(
            patient=batch.patient,
            browser_notifications_enabled=True,
        ).exists()
        if enabled:
            deliveries = [
                PushDelivery(notification=notification, subscription=subscription)
                for subscription in PushSubscription.objects.filter(patient=batch.patient, active=True)
            ]
            PushDelivery.objects.bulk_create(deliveries, ignore_conflicts=True)
            for delivery in deliveries:
                transaction.on_commit(partial(dispatch, delivery.pk))
    return notification


def safe_create_task_notification(batch_id):
    try:
        return create_task_notification(batch_id)
    except Exception:
        logger.warning(
            "Task notification creation failed",
            extra={"error_code": "notification_creation_failed"},
        )
        return None


def _validate_subscription(endpoint, p256dh, auth):
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise InvalidPushSubscription("Invalid push endpoint")
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise InvalidPushSubscription("Invalid push endpoint") from exc
    allowed_hosts = getattr(settings, "WEBPUSH_ALLOWED_ENDPOINT_HOSTS", ())
    host_allowed = any(
        host == rule or (rule.startswith(".") and host.endswith(rule) and host != rule[1:])
        for rule in allowed_hosts
        if isinstance(rule, str) and rule
    )
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in {None, 443}
        or not host_allowed
    ):
        raise InvalidPushSubscription("Invalid push endpoint")
    if _KEY_VALUE.fullmatch(p256dh or "") is None or _KEY_VALUE.fullmatch(auth or "") is None:
        raise InvalidPushSubscription("Invalid push subscription keys")


def upsert_push_subscription(patient, endpoint, p256dh, auth, *, browser_family="unknown"):
    _validate_subscription(endpoint, p256dh, auth)
    browser_family = browser_family if browser_family in _BROWSERS else "unknown"
    digest = endpoint_hash(endpoint)
    values = {
        "endpoint_ciphertext": encrypt_subscription_value(endpoint, "endpoint"),
        "p256dh_ciphertext": encrypt_subscription_value(p256dh, "p256dh"),
        "auth_ciphertext": encrypt_subscription_value(auth, "auth"),
        "browser_family": browser_family,
        "active": True,
    }
    with transaction.atomic():
        subscription, _created = PushSubscription.objects.update_or_create(
            patient=patient,
            endpoint_hash=digest,
            defaults=values,
        )
    return subscription


def revoke_push_subscriptions(patient, *, endpoint=None):
    subscriptions = PushSubscription.objects.filter(patient=patient)
    if endpoint is not None:
        if not isinstance(endpoint, str) or not endpoint:
            return 0
        subscriptions = subscriptions.filter(endpoint_hash=endpoint_hash(endpoint))
    with transaction.atomic():
        # Delivery claims hold this parent before Subscription -> Delivery;
        # Django's cascading delete visits Delivery -> Subscription instead.
        # The shared parent guard keeps those child orders from interleaving.
        if not Patient.objects.select_for_update().filter(pk=patient.pk).exists():
            return 0
        count = subscriptions.count()
        subscriptions.delete()
        return count


def _subscription_info(subscription):
    return {
        "endpoint": decrypt_subscription_value(subscription.endpoint_ciphertext, "endpoint"),
        "keys": {
            "p256dh": decrypt_subscription_value(subscription.p256dh_ciphertext, "p256dh"),
            "auth": decrypt_subscription_value(subscription.auth_ciphertext, "auth"),
        },
    }


def _finish_delivery(delivery_id, *, status, now, error_code="", retry_delay=None):
    with transaction.atomic():
        delivery = PushDelivery.objects.select_for_update().filter(pk=delivery_id).first()
        if delivery is None:
            return PushDeliveryResult(PushDeliveryStatus.FAILED)
        delivery.status = status
        delivery.error_code = error_code
        delivery.sent_at = now if status == PushDeliveryStatus.SENT else None
        delivery.next_attempt_at = now + timedelta(seconds=retry_delay) if retry_delay else None
        delivery.save(
            update_fields=["status", "error_code", "sent_at", "next_attempt_at", "updated_at"]
        )
    return PushDeliveryResult(status, retry_delay)


def deliver_push(delivery_id, *, sender=None, now=None):
    now = now or timezone.now()
    sender = sender or get_webpush_sender()
    with transaction.atomic():
        identity = PushDelivery.objects.filter(pk=delivery_id).values(
            "subscription_id", "subscription__patient_id", "subscription__patient__account_id"
        ).first()
        if identity is None:
            return PushDeliveryResult(PushDeliveryStatus.FAILED)
        patient = _lock_notification_patient(
            identity["subscription__patient_id"], identity["subscription__patient__account_id"]
        )
        if patient is None:
            return PushDeliveryResult(PushDeliveryStatus.FAILED)
        subscription = PushSubscription.objects.select_for_update().filter(
            pk=identity["subscription_id"], patient=patient
        ).first()
        if subscription is None:
            return PushDeliveryResult(PushDeliveryStatus.FAILED)
        subscription.patient = patient
        delivery = (
            PushDelivery.objects.select_for_update(of=("self",))
            .select_related("notification")
            .filter(pk=delivery_id, subscription=subscription)
            .first()
        )
        if delivery is None:
            return PushDeliveryResult(PushDeliveryStatus.FAILED)
        delivery.subscription = subscription
        if delivery.status == PushDeliveryStatus.SENT:
            return PushDeliveryResult(PushDeliveryStatus.SENT)
        if (
            delivery.status == PushDeliveryStatus.SENDING
            and delivery.updated_at > now - timedelta(minutes=5)
        ):
            return PushDeliveryResult(PushDeliveryStatus.SENDING)
        preference_enabled = PatientPreference.objects.filter(
            patient=delivery.subscription.patient,
            browser_notifications_enabled=True,
        ).exists()
        if not delivery.subscription.active or not delivery.subscription.patient.account.is_active or not preference_enabled:
            delivery.status = PushDeliveryStatus.FAILED
            delivery.error_code = "subscription_disabled"
            delivery.next_attempt_at = None
            delivery.save(update_fields=["status", "error_code", "next_attempt_at", "updated_at"])
            return PushDeliveryResult(PushDeliveryStatus.FAILED)
        delivery.status = PushDeliveryStatus.SENDING
        delivery.attempt_count = min(delivery.attempt_count + 1, 65535)
        delivery.error_code = ""
        delivery.next_attempt_at = None
        delivery.save(
            update_fields=["status", "attempt_count", "error_code", "next_attempt_at", "updated_at"]
        )
        subscription_info = _subscription_info(delivery.subscription)
        payload = serialize_push_notification(delivery.notification)
        attempt_count = delivery.attempt_count
        subscription_id = delivery.subscription_id
    try:
        sender.send(subscription_info, payload)
    except PushSubscriptionGone:
        PushSubscription.objects.filter(pk=subscription_id).update(active=False)
        return _finish_delivery(
            delivery_id,
            status=PushDeliveryStatus.FAILED,
            now=now,
            error_code="subscription_gone",
        )
    except Exception as exc:
        safe_record_metric(
            "phr_provider_error_total",
            {"provider": "webpush", "error_type": "unavailable"},
        )
        if not isinstance(exc, PushDeliveryUnavailable):
            logger.warning("Push sender failed", extra={"error_code": "push_sender_failed"})
        if attempt_count >= len(RETRY_DELAYS):
            return _finish_delivery(
                delivery_id,
                status=PushDeliveryStatus.FAILED,
                now=now,
                error_code="delivery_exhausted",
            )
        retry_delay = RETRY_DELAYS[attempt_count - 1]
        return _finish_delivery(
            delivery_id,
            status=PushDeliveryStatus.RETRY,
            now=now,
            error_code="delivery_unavailable",
            retry_delay=retry_delay,
        )
    PushSubscription.objects.filter(pk=subscription_id).update(last_success_at=now)
    return _finish_delivery(delivery_id, status=PushDeliveryStatus.SENT, now=now)


def due_push_deliveries(*, now=None, limit=100):
    now = now or timezone.now()
    return tuple(
        PushDelivery.objects.filter(
            Q(status=PushDeliveryStatus.PENDING)
            | Q(status=PushDeliveryStatus.RETRY, next_attempt_at__lte=now)
            | Q(status=PushDeliveryStatus.SENDING, updated_at__lte=now - timedelta(minutes=5))
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[: max(1, min(int(limit), 1000))]
    )
