from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import threading

from django.db import close_old_connections, connection
from django.utils import timezone
import pytest

from apps.accounts.authentication import (
    EnrollmentUnavailable,
    ExistingAccountRequiresLogin,
    create_or_upgrade_account,
)
from apps.accounts.crypto import encrypt_phone, hash_phone
from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion
from apps.accounts.models import (
    Account,
    AccountDeletionJob,
    OtpChallenge,
    OtpThrottle,
)
from apps.accounts.services import LockedOtp, request_otp
from apps.accounts.session_registry import revoke_account_sessions
from tests.accounts.fakes import RecordingSmsProvider
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture(autouse=True)
def require_postgresql_url():
    if not os.environ.get("PHR_POSTGRES_TEST_URL"):
        pytest.skip("PHR_POSTGRES_TEST_URL is unavailable")
    assert connection.vendor == "postgresql", (
        "Run with --ds=config.settings.postgres_test when "
        "PHR_POSTGRES_TEST_URL is available"
    )


def _thread_call(operation):
    close_old_connections()
    try:
        return operation()
    finally:
        close_old_connections()


def test_same_phone_first_use_and_reset_request_complete_without_deadlock():
    canonical_phone = "+8613800138000"
    account = Account.objects.create_user(
        phone_hash=hash_phone(canonical_phone),
        phone_encrypted=encrypt_phone(canonical_phone),
        password="Existing strong passphrase 2026",
    )
    first_use_challenge = OtpChallenge.objects.create(
        phone_hash=account.phone_hash,
        phone_encrypted=account.phone_encrypted,
        ip_hash="f" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )
    ready = threading.Barrier(2, timeout=10)

    def first_use_transition():
        ready.wait()
        try:
            create_or_upgrade_account(
                first_use_challenge,
                "Replacement strong passphrase 2026",
            )
        except ExistingAccountRequiresLogin:
            return "existing-account"
        return "unexpected-upgrade"

    def reset_request():
        ready.wait()
        challenge = request_otp(
            "13800138000",
            "203.0.113.1",
            RecordingSmsProvider(),
            purpose=OtpChallenge.Purpose.PASSWORD_RESET,
            account=account,
        )
        return challenge.purpose

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_use_future = executor.submit(_thread_call, first_use_transition)
        reset_future = executor.submit(_thread_call, reset_request)
        results = {
            first_use_future.result(timeout=15),
            reset_future.result(timeout=15),
        }

    assert results == {
        "existing-account",
        OtpChallenge.Purpose.PASSWORD_RESET,
    }


def test_same_phone_first_use_and_account_purge_complete_without_deadlock():
    canonical_phone = "+8613900139000"
    account = Account.objects.create_user(
        phone_hash=hash_phone(canonical_phone),
        phone_encrypted=encrypt_phone(canonical_phone),
        password="Existing strong passphrase 2026",
        is_active=False,
    )
    job = AccountDeletionJob.objects.create(account=account)
    challenge = OtpChallenge.objects.create(
        phone_hash=account.phone_hash,
        phone_encrypted=account.phone_encrypted,
        ip_hash="e" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )
    OtpThrottle.objects.create(
        scope="phone",
        identifier_hash=account.phone_hash,
    )
    ready = threading.Barrier(2, timeout=10)

    def first_use_transition():
        ready.wait()
        try:
            create_or_upgrade_account(
                challenge,
                "Replacement strong passphrase 2026",
            )
        except EnrollmentUnavailable:
            return "enrollment-unavailable"
        return "unexpected-upgrade"

    def account_purge():
        ready.wait()
        return purge_account_deletion(job.pk).outcome

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_use_future = executor.submit(_thread_call, first_use_transition)
        purge_future = executor.submit(_thread_call, account_purge)
        results = {
            first_use_future.result(timeout=15),
            purge_future.result(timeout=15),
        }

    assert results == {
        "enrollment-unavailable",
        AccountDeletionOutcome.PURGED,
    }
    assert not Account.objects.filter(pk=account.pk).exists()
    assert not OtpChallenge.objects.filter(pk=challenge.pk).exists()
    assert not OtpThrottle.objects.filter(
        scope="phone",
        identifier_hash=account.phone_hash,
    ).exists()


def test_first_use_request_waiting_on_purge_never_continues_without_phone_mutex(
    monkeypatch,
):
    canonical_phone = "+8613400134000"
    account = Account.objects.create_user(
        phone_hash=hash_phone(canonical_phone),
        phone_encrypted=encrypt_phone(canonical_phone),
        password="Existing strong passphrase 2026",
        is_active=False,
    )
    job = AccountDeletionJob.objects.create(account=account)
    OtpThrottle.objects.create(
        scope="phone",
        identifier_hash=account.phone_hash,
    )
    provider = RecordingSmsProvider()
    purge_holds_locks = threading.Event()
    release_purge = threading.Event()
    purge_pid_ready = threading.Event()
    request_pid_ready = threading.Event()
    backend_pids = {}

    def capture_backend_pid(role, ready):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_pids[role] = cursor.fetchone()[0]
        ready.set()

    def pause_purge_after_same_phone_locks(account_id):
        purge_holds_locks.set()
        assert release_purge.wait(timeout=15), "lock monitor did not release purge"
        return revoke_account_sessions(account_id)

    monkeypatch.setattr(
        "apps.accounts.deletion.revoke_account_sessions",
        pause_purge_after_same_phone_locks,
    )

    def account_purge():
        capture_backend_pid("purge", purge_pid_ready)
        return purge_account_deletion(job.pk).outcome

    def first_use_request():
        capture_backend_pid("request", request_pid_ready)
        try:
            request_otp(
                "13400134000",
                "203.0.113.2",
                provider,
                purpose=OtpChallenge.Purpose.FIRST_USE,
            )
        except LockedOtp:
            return "locked"
        return "unexpected-challenge"

    executor = ThreadPoolExecutor(max_workers=2)
    monitor_connection = None
    try:
        purge_future = executor.submit(_thread_call, account_purge)
        assert purge_pid_ready.wait(timeout=10), "purge backend PID was not captured"
        assert purge_holds_locks.wait(timeout=10), "purge did not reach its locked pause"
        request_future = executor.submit(_thread_call, first_use_request)
        assert request_pid_ready.wait(timeout=10), "request backend PID was not captured"

        purge_pid = backend_pids["purge"]
        request_pid = backend_pids["request"]
        monitor_connection = connection.copy(alias="authentication_lock_monitor")
        with monitor_connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            monitor_pid = cursor.fetchone()[0]
        assert len({purge_pid, request_pid, monitor_pid}) == 3

        def fetch_request_wait_state(pid):
            with monitor_connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT wait_event_type, pg_blocking_pids(pid)
                    FROM pg_stat_activity
                    WHERE pid = %s
                    """,
                    [pid],
                )
                return cursor.fetchone()

        wait_until_backend_is_blocked_by(
            fetch_request_wait_state,
            request_pid=request_pid,
            blocker_pid=purge_pid,
            timeout_seconds=10,
        )
        release_purge.set()
        assert {
            purge_future.result(timeout=15),
            request_future.result(timeout=15),
        } == {
            AccountDeletionOutcome.PURGED,
            "locked",
        }
    finally:
        # Release the lock holder before closing the independent monitor and
        # joining workers, including every timeout or assertion-failure path.
        release_purge.set()
        try:
            if monitor_connection is not None:
                monitor_connection.close()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    assert provider.codes == []
    assert not Account.objects.filter(pk=account.pk).exists()
    assert not OtpChallenge.objects.filter(phone_hash=account.phone_hash).exists()
    assert not OtpThrottle.objects.filter(
        scope="phone",
        identifier_hash=account.phone_hash,
    ).exists()
