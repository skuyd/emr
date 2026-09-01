from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time

from apps.accounts.crypto import InvalidCiphertext, decrypt_phone, encrypt_phone, hash_ip, hash_phone
from apps.accounts.models import Account, OtpChallenge, PasswordAttemptThrottle
from apps.accounts.services import (
    LockedOtp,
    ThrottledPassword,
    consume_otp,
    enforce_password_attempt_limits,
    request_otp,
)
from tests.accounts.fakes import RecordingSmsProvider


@override_settings(ACCOUNTS_CRYPTO_SECRET="test-encryption-secret")
def test_phone_encryption_round_trips_and_tampering_is_rejected():
    encrypted = encrypt_phone("+8613800138000")
    assert encrypted.startswith("v1:")
    assert decrypt_phone(encrypted) == "+8613800138000"
    with pytest.raises(InvalidCiphertext):
        decrypt_phone(encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B"))


@override_settings(ACCOUNTS_CRYPTO_SECRET="test-encryption-secret")
def test_phone_and_ip_hashes_are_domain_separated():
    assert hash_phone("+8613800138000") == hash_phone("+8613800138000")
    assert hash_phone("203.0.113.1") != hash_ip("203.0.113.1")


@freeze_time("2026-08-30 08:00:00")
def test_first_use_consumption_leaves_account_creation_to_the_following_flow(db):
    provider = RecordingSmsProvider()
    first = request_otp(
        "13800138000",
        "203.0.113.1",
        provider,
        purpose=OtpChallenge.Purpose.FIRST_USE,
    )
    consumed = consume_otp(first.pk, provider.last_code, purpose=OtpChallenge.Purpose.FIRST_USE)

    assert Account.objects.count() == 0
    assert consumed.phone_hash == first.phone_hash
    assert "+8613800138000" not in consumed.phone_encrypted
    assert consumed.phone_hash not in str(consumed)
    assert first.otp_hash != provider.last_code


def test_sign_in_challenge_requires_matching_active_account(db):
    account = Account.objects.create(phone_hash=hash_phone("+8613800138000"), phone_encrypted="ciphertext")
    provider = RecordingSmsProvider()

    challenge = request_otp(
        "13800138000",
        "203.0.113.1",
        provider,
        purpose=OtpChallenge.Purpose.SIGN_IN,
        account=account,
    )

    assert challenge.account_id == account.pk
    assert provider.last_purpose == OtpChallenge.Purpose.SIGN_IN
    with pytest.raises(LockedOtp):
        consume_otp(challenge.pk, provider.last_code, purpose=OtpChallenge.Purpose.SIGN_IN)
    assert consume_otp(
        challenge.pk,
        provider.last_code,
        purpose=OtpChallenge.Purpose.SIGN_IN,
        account_id=account.pk,
    ).consumed_at is not None


def test_sign_in_challenge_rejects_a_different_or_inactive_account(db):
    account = Account.objects.create(phone_hash=hash_phone("+8613800138000"), phone_encrypted="ciphertext")
    other = Account.objects.create(phone_hash=hash_phone("+8613900138000"), phone_encrypted="other")
    provider = RecordingSmsProvider()

    with pytest.raises(LockedOtp):
        request_otp(
            "13800138000",
            "203.0.113.1",
            provider,
            purpose=OtpChallenge.Purpose.SIGN_IN,
            account=other,
        )

    account.is_active = False
    account.save(update_fields=["is_active"])
    with pytest.raises(LockedOtp):
        request_otp(
            "13800138000",
            "203.0.113.1",
            provider,
            purpose=OtpChallenge.Purpose.SIGN_IN,
            account=account,
        )


@freeze_time("2026-08-30 08:00:00")
def test_password_attempt_limits_use_a_fifteen_minute_window_and_reset_after_success(db):
    phone_hash = hash_phone("+8613800138000")
    ip_hash = hash_ip("203.0.113.1")
    start = timezone.now()

    for _ in range(5):
        enforce_password_attempt_limits(phone_hash, ip_hash)
    with pytest.raises(ThrottledPassword):
        enforce_password_attempt_limits(phone_hash, ip_hash)

    phone_row = PasswordAttemptThrottle.objects.get(scope="phone", identifier_hash=phone_hash)
    assert phone_row.attempts == 5
    with freeze_time(start + timedelta(minutes=15)):
        enforce_password_attempt_limits(phone_hash, ip_hash)
    phone_row.refresh_from_db()
    assert phone_row.attempts == 1

    enforce_password_attempt_limits(phone_hash, ip_hash, succeeded=True)
    phone_row.refresh_from_db()
    assert phone_row.attempts == 0
    ip_row = PasswordAttemptThrottle.objects.get(scope="ip", identifier_hash=ip_hash)
    assert ip_row.attempts == 1


def test_password_attempt_ip_limit_does_not_disclose_which_scope_locked(db):
    ip_hash = hash_ip("203.0.113.1")
    for suffix in range(30):
        enforce_password_attempt_limits(hash_phone(f"+86138{suffix:06d}"), ip_hash)

    with pytest.raises(ThrottledPassword) as raised:
        enforce_password_attempt_limits(hash_phone("+8613999999999"), ip_hash)

    assert "phone" not in str(raised.value).lower()
    assert "ip" not in str(raised.value).lower()


def test_models_and_errors_do_not_expose_raw_identifiers(db):
    provider = RecordingSmsProvider()
    challenge = request_otp("13800138000", "203.0.113.1", provider, purpose=OtpChallenge.Purpose.FIRST_USE)

    assert "13800138000" not in repr(challenge)
    assert "203.0.113.1" not in repr(challenge)
    assert "13800138000" not in str(challenge)
    assert "203.0.113.1" not in str(challenge)
    assert OtpChallenge.objects.count() == 1
