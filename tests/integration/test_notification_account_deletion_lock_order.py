"""Exercise the account deletion / notification boundary on real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading

from django.db import close_old_connections, connection
import pytest

from apps.accounts.deletion import request_account_deletion
from apps.accounts.models import Account
from apps.documents.models import UploadBatch
from apps.notifications.models import PushDelivery
from apps.notifications.services import (
    create_task_notification, deliver_push, revoke_push_subscriptions, upsert_push_subscription,
)
from apps.patients.models import Patient
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.notifications.test_services import make_completed_batch
from tests.notifications.test_webpush import AUTH, ENDPOINT, P256DH


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.mark.parametrize("operation", ["create", "deliver"])
def test_account_deletion_and_notification_creation_do_not_reverse_parent_locks(operation):
    if not os.environ.get("PHR_POSTGRES_TEST_URL"):
        pytest.skip("PHR_POSTGRES_TEST_URL is unavailable")
    assert connection.vendor == "postgresql"
    account = Account.objects.create_user(phone_hash="9" * 64, phone_encrypted="synthetic")
    patient = Patient.objects.create(account=account, display_name="Synthetic")
    batch = UploadBatch.objects.create(patient=patient)
    notify = lambda: create_task_notification(batch.pk)
    if operation == "deliver":
        completed = make_completed_batch(patient, failed=True)
        notification = create_task_notification(completed.pk, dispatch=lambda _pk: None)
        subscription = upsert_push_subscription(patient, ENDPOINT, P256DH, AUTH)
        delivery = PushDelivery.objects.create(notification=notification, subscription=subscription)

        class NeverSend:
            def send(self, *_args):
                pytest.fail("Deleted accounts must not send push notifications")

        notify = lambda: deliver_push(delivery.pk, sender=NeverSend())
    account_locked, resume_deletion = threading.Event(), threading.Event()
    notification_started = threading.Event()
    pids = {}

    def thread_call(role, action):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[role] = cursor.fetchone()[0]
            if role == "notification":
                notification_started.set()
            return action()
        finally:
            close_old_connections()

    def pause_after_patient_guard(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if 'FROM "patients_patient"' in sql and "FOR UPDATE" in sql and not account_locked.is_set():
            account_locked.set()
            assert resume_deletion.wait(10), "Notification lock monitor did not release deletion"
        return result

    def delete_account():
        with connection.execute_wrapper(pause_after_patient_guard):
            return request_account_deletion(
                account.pk, document_dispatch=lambda _pk: None, account_dispatch=lambda _pk: None
            )

    def wait_state(pid):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = %s",
                [pid],
            )
            return cursor.fetchone()

    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(thread_call, "deletion", delete_account)
        try:
            assert account_locked.wait(10)
            notifying = pool.submit(thread_call, "notification", notify)
            assert notification_started.wait(10)
            wait_until_backend_is_blocked_by(
                wait_state, request_pid=pids["notification"], blocker_pid=pids["deletion"]
            )
        finally:
            resume_deletion.set()
        deleting.result(timeout=10)
        result = notifying.result(timeout=10)
        assert result is None if operation == "create" else result.status == "FAILED"
    account.refresh_from_db()
    assert account.is_active is False


def test_subscription_revocation_and_delivery_do_not_reverse_child_locks():
    if not os.environ.get("PHR_POSTGRES_TEST_URL"):
        pytest.skip("PHR_POSTGRES_TEST_URL is unavailable")
    assert connection.vendor == "postgresql"
    account = Account.objects.create_user(phone_hash="8" * 64, phone_encrypted="synthetic")
    patient = Patient.objects.create(account=account, display_name="Synthetic")
    batch = make_completed_batch(patient, failed=True)
    notification = create_task_notification(batch.pk, dispatch=lambda _pk: None)
    subscription = upsert_push_subscription(patient, ENDPOINT, P256DH, AUTH)
    delivery = PushDelivery.objects.create(notification=notification, subscription=subscription)
    child_deleted, release_revocation, delivery_started = (
        threading.Event(), threading.Event(), threading.Event()
    )
    pids = {}

    def thread_call(role, action):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                pids[role] = cursor.fetchone()[0]
            if role == "delivery":
                delivery_started.set()
            return action()
        finally:
            close_old_connections()

    def pause_after_child_delete(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.startswith('DELETE FROM "notifications_pushdelivery"'):
            child_deleted.set()
            assert release_revocation.wait(10)
        return result

    def revoke():
        with connection.execute_wrapper(pause_after_child_delete):
            return revoke_push_subscriptions(patient)

    def wait_state(pid):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT wait_event_type, pg_blocking_pids(pid) FROM pg_stat_activity WHERE pid = %s",
                [pid],
            )
            return cursor.fetchone()

    class NeverSend:
        def send(self, *_args):
            pytest.fail("Revoked subscriptions must not send push notifications")

    with ThreadPoolExecutor(max_workers=2) as pool:
        revoking = pool.submit(thread_call, "revocation", revoke)
        try:
            assert child_deleted.wait(10)
            sending = pool.submit(thread_call, "delivery", lambda: deliver_push(delivery.pk, sender=NeverSend()))
            assert delivery_started.wait(10)
            wait_until_backend_is_blocked_by(
                wait_state, request_pid=pids["delivery"], blocker_pid=pids["revocation"]
            )
        finally:
            release_revocation.set()
        assert revoking.result(timeout=10) == 1
        assert sending.result(timeout=10).status == "FAILED"
