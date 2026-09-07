import json
import re
import uuid

import pytest
from django.test import Client

from apps.notifications.models import PushSubscription
from apps.notifications.services import create_task_notification
from apps.patients.models import Patient
from apps.patients.services import create_patient_space
from tests.notifications.test_services import make_completed_batch
from tests.notifications.test_webpush import AUTH, ENDPOINT, P256DH


def make_patient(django_user_model):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
    )
    patient = create_patient_space(
        account,
        "测试用户",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "notification-view-test"},
    )
    return account, patient


@pytest.mark.django_db
def test_notification_api_is_authenticated_patient_scoped_and_no_store(client, django_user_model):
    account, patient = make_patient(django_user_model)
    _foreign_account, foreign = make_patient(django_user_model)
    own = create_task_notification(make_completed_batch(patient).pk)
    foreign_notice = create_task_notification(make_completed_batch(foreign).pk)

    anonymous = client.get("/api/notifications/")
    assert anonymous.status_code == 302

    client.force_login(account)
    response = client.get("/api/notifications/")

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store, private"
    body = response.json()
    assert body["unread_count"] == 1
    assert [item["notification_id"] for item in body["notifications"]] == [str(own.pk)]
    assert str(foreign_notice.pk) not in response.content.decode("utf-8")


@pytest.mark.django_db
def test_notification_center_keeps_unread_badge_open_url_and_task_destination(client, django_user_model):
    account, patient = make_patient(django_user_model)
    notification = create_task_notification(make_completed_batch(patient).pk)
    client.force_login(account)

    content = client.get("/").content.decode()

    assert "data-notification-center" in content
    assert "data-notification-toggle" in content
    assert 'aria-controls="notification-panel"' in content
    assert 'aria-label="任务通知，1 条未读"' in content
    assert "data-notification-badge" in content
    assert f'data-notification-id="{notification.pk}"' in content
    assert f'href="/notifications/{notification.pk}/open/"' in content
    assert "data-notification-list" in content
    assert "data-notification-toast" in content
    assert 'role="status"' in content and 'aria-live="polite"' in content
    assert re.search(r'href="/tasks/(?:\?patient=[0-9a-f-]+)?"', content)


@pytest.mark.django_db
def test_notification_center_zero_state_hides_badge_but_keeps_visible_label(client, django_user_model):
    account, _patient = make_patient(django_user_model)
    client.force_login(account)

    content = client.get("/").content.decode()
    badge = re.search(r'<span class="notification-badge"([^>]*)>', content)

    assert badge is not None and "hidden" in badge.group(1)
    assert '<span class="notification-toggle__label">通知</span>' in content
    assert 'aria-label="任务通知，无未读"' in content


@pytest.mark.django_db
def test_mark_read_and_open_reject_foreign_notification(client, django_user_model):
    account, _patient = make_patient(django_user_model)
    _foreign_account, foreign = make_patient(django_user_model)
    foreign_notice = create_task_notification(make_completed_batch(foreign).pk)
    client.force_login(account)

    assert client.post(f"/api/notifications/{foreign_notice.pk}/read/").status_code == 404
    assert client.get(f"/notifications/{foreign_notice.pk}/open/").status_code == 404


@pytest.mark.django_db
def test_subscription_endpoint_requires_post_auth_and_valid_json(client, django_user_model):
    account, _patient = make_patient(django_user_model)

    assert client.get("/api/push-subscriptions/").status_code == 302
    client.force_login(account)
    assert client.get("/api/push-subscriptions/").status_code == 405
    invalid = client.post(
        "/api/push-subscriptions/",
        data=json.dumps({"endpoint": "http://not-secure.test"}),
        content_type="application/json",
    )
    assert invalid.status_code == 400

    response = client.post(
        "/api/push-subscriptions/",
        data=json.dumps(
            {
                "endpoint": ENDPOINT,
                "keys": {"p256dh": P256DH, "auth": AUTH},
                "browser_family": "chrome",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 201
    assert PushSubscription.objects.filter(patient__account=account).count() == 1


@pytest.mark.django_db
def test_subscription_endpoint_enforces_csrf(django_user_model):
    account, _patient = make_patient(django_user_model)
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(account)

    response = csrf_client.post(
        "/api/push-subscriptions/",
        data=json.dumps(
            {
                "endpoint": ENDPOINT,
                "keys": {"p256dh": P256DH, "auth": AUTH},
                "browser_family": "chrome",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 403
    assert not PushSubscription.objects.filter(patient__account=account).exists()
