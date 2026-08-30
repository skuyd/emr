import json
from types import SimpleNamespace
import uuid

from django.test import override_settings
import pytest
import pywebpush

from apps.notifications.models import PushDelivery, PushDeliveryStatus, PushSubscription
from apps.notifications.services import (
    create_task_notification,
    deliver_push,
    InvalidPushSubscription,
    revoke_push_subscriptions,
    upsert_push_subscription,
)
from apps.notifications.webpush import PyWebPushSender, PushSubscriptionGone
from apps.patients.models import Patient, PatientPreference
from tests.notifications.test_services import FORBIDDEN_MARKERS, make_completed_batch


ENDPOINT = "https://push.example.test/subscriptions/opaque-token"
P256DH = "BNcW8V8wLwVhZk5wYlN5dGhldGljS2V5VGhhdElzTG9uZ0Vub3VnaA"
AUTH = "c3ludGhldGljLWF1dGg"


def make_patient(django_user_model):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
    )
    patient = Patient.objects.create(account=account, display_name="测试用户")
    PatientPreference.objects.create(patient=patient, browser_notifications_enabled=True)
    return account, patient


@pytest.mark.django_db
def test_push_subscription_secrets_are_encrypted_and_upsert_is_idempotent(django_user_model):
    _account, patient = make_patient(django_user_model)

    first = upsert_push_subscription(patient, ENDPOINT, P256DH, AUTH, browser_family="chrome")
    second = upsert_push_subscription(patient, ENDPOINT, P256DH, AUTH, browser_family="edge")
    stored = PushSubscription.objects.get(pk=first.pk)

    assert first.pk == second.pk
    assert PushSubscription.objects.count() == 1
    assert stored.endpoint_hash != ENDPOINT
    assert ENDPOINT not in stored.endpoint_ciphertext
    assert P256DH not in stored.p256dh_ciphertext
    assert AUTH not in stored.auth_ciphertext
    assert stored.browser_family == "edge"


@pytest.mark.django_db
def test_push_delivery_contains_only_generic_allowlisted_payload(django_user_model):
    _account, patient = make_patient(django_user_model)
    subscription = upsert_push_subscription(patient, ENDPOINT, P256DH, AUTH)
    batch = make_completed_batch(
        patient,
        filename="FORBIDDEN_FILENAME-FORBIDDEN_INDICATOR-987.654-FORBIDDEN_DIAGNOSIS.png",
    )
    notification = create_task_notification(batch.pk, dispatch=lambda _delivery_id: True)
    delivery = PushDelivery.objects.get(notification=notification, subscription=subscription)
    captured = {}

    class Sender:
        def send(self, subscription_info, payload):
            captured["subscription"] = subscription_info
            captured["payload"] = payload

    result = deliver_push(delivery.pk, sender=Sender())

    delivery.refresh_from_db()
    assert result.status == PushDeliveryStatus.SENT
    assert delivery.status == PushDeliveryStatus.SENT
    assert captured["subscription"] == {
        "endpoint": ENDPOINT,
        "keys": {"p256dh": P256DH, "auth": AUTH},
    }
    assert set(captured["payload"]) == {"notification_id", "title", "body"}
    serialized = json.dumps(captured["payload"], ensure_ascii=False)
    assert all(marker not in serialized for marker in FORBIDDEN_MARKERS)


@pytest.mark.django_db
def test_revoke_push_subscription_is_patient_scoped(django_user_model):
    _account, patient = make_patient(django_user_model)
    _foreign_account, foreign = make_patient(django_user_model)
    own = upsert_push_subscription(patient, ENDPOINT, P256DH, AUTH)
    foreign_subscription = upsert_push_subscription(
        foreign,
        "https://push.example.test/subscriptions/foreign-token",
        P256DH,
        AUTH,
    )

    assert revoke_push_subscriptions(patient, endpoint=ENDPOINT) == 1
    assert not PushSubscription.objects.filter(pk=own.pk).exists()
    assert PushSubscription.objects.filter(pk=foreign_subscription.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/subscription",
        "https://localhost/subscription",
        "https://push.example.test:8443/subscription",
        "https://push.example.test.evil.invalid/subscription",
        "https://user@push.example.test/subscription",
    ],
)
def test_push_endpoint_rejects_ssrf_targets(django_user_model, endpoint):
    _account, patient = make_patient(django_user_model)

    with pytest.raises(InvalidPushSubscription):
        upsert_push_subscription(patient, endpoint, P256DH, AUTH)


@override_settings(
    WEBPUSH_ENABLED=True,
    WEBPUSH_VAPID_PRIVATE_KEY="private-key",
    WEBPUSH_VAPID_SUBJECT="mailto:operations@example.invalid",
    WEBPUSH_TTL_SECONDS=300,
    WEBPUSH_TIMEOUT_SECONDS=10,
)
def test_pywebpush_sender_passes_bounded_provider_options(monkeypatch):
    captured = {}

    def fake_webpush(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)

    PyWebPushSender().send(
        {"endpoint": ENDPOINT, "keys": {"p256dh": P256DH, "auth": AUTH}},
        {"notification_id": str(uuid.uuid4()), "title": "资料整理完成", "body": "你上传的资料已整理完成，点击查看结果。"},
    )

    assert captured["ttl"] == 300
    assert captured["timeout"] == 10
    assert captured["vapid_claims"] == {"sub": "mailto:operations@example.invalid"}
    assert json.loads(captured["data"])["title"] == "资料整理完成"


@override_settings(
    WEBPUSH_ENABLED=True,
    WEBPUSH_VAPID_PRIVATE_KEY="private-key",
    WEBPUSH_VAPID_SUBJECT="mailto:operations@example.invalid",
    WEBPUSH_TTL_SECONDS=300,
    WEBPUSH_TIMEOUT_SECONDS=10,
)
def test_pywebpush_sender_maps_expired_subscription(monkeypatch):
    def gone(**_kwargs):
        response = SimpleNamespace(status_code=410)
        raise pywebpush.WebPushException("gone", response=response)

    monkeypatch.setattr(pywebpush, "webpush", gone)

    with pytest.raises(PushSubscriptionGone):
        PyWebPushSender().send(
            {"endpoint": ENDPOINT, "keys": {"p256dh": P256DH, "auth": AUTH}},
            {"notification_id": str(uuid.uuid4()), "title": "资料整理完成", "body": "你上传的资料已整理完成，点击查看结果。"},
        )
