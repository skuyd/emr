from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import threading

from django.db import close_old_connections, connection
from django.utils import timezone
import pytest

from apps.accounts.authentication import (
    ExistingAccountRequiresLogin,
    create_or_upgrade_account,
)
from apps.accounts.crypto import encrypt_phone, hash_phone
from apps.accounts.models import Account, OtpChallenge
from apps.accounts.services import request_otp
from tests.accounts.fakes import RecordingSmsProvider


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
