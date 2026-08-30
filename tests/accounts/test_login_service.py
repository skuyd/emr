from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time

from apps.accounts.crypto import InvalidCiphertext, decrypt_phone, encrypt_phone, hash_ip, hash_phone
from apps.accounts.models import Account, OtpChallenge
from apps.accounts.services import request_otp, verify_otp
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
def test_first_success_creates_account_and_later_success_reuses_it(db):
    provider = RecordingSmsProvider()
    first = request_otp("13800138000", "203.0.113.1", provider)
    account = verify_otp("13800138000", provider.last_code)

    assert Account.objects.count() == 1
    assert account.phone_hash == first.phone_hash
    assert "+8613800138000" not in account.phone_encrypted
    assert first.otp_hash != provider.last_code

    provider = RecordingSmsProvider()
    with freeze_time(timezone.now() + timedelta(seconds=60)):
        request_otp("13800138000", "203.0.113.1", provider)
        assert verify_otp("13800138000", provider.last_code).pk == account.pk
    assert Account.objects.count() == 1


def test_models_and_errors_do_not_expose_raw_identifiers(db):
    provider = RecordingSmsProvider()
    challenge = request_otp("13800138000", "203.0.113.1", provider)

    assert "13800138000" not in repr(challenge)
    assert "203.0.113.1" not in repr(challenge)
    assert "13800138000" not in str(challenge)
    assert "203.0.113.1" not in str(challenge)
    assert OtpChallenge.objects.count() == 1
