from datetime import timedelta

import pytest
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.accounts.authentication import (
    ExistingAccountRequiresLogin,
    complete_verified_password_reset,
    create_or_upgrade_account,
)
from apps.accounts.crypto import encrypt_phone, hash_phone
from apps.accounts.models import Account, OtpChallenge, OtpThrottle
from apps.accounts.services import request_otp
from tests.accounts.fakes import RecordingSmsProvider


PHONE = "13800138000"
CANONICAL_PHONE = "+8613800138000"


def _account(*, password="Existing strong passphrase 2026"):
    return Account.objects.create_user(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
        password=password,
    )


def _track_shared_lock_order(monkeypatch):
    order = []
    original = QuerySet.select_for_update

    def tracked(queryset, *args, **kwargs):
        if queryset.model in (OtpThrottle, Account):
            order.append(queryset.model)
        return original(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked)
    return order


@pytest.mark.django_db
@pytest.mark.parametrize(
    "purpose",
    [OtpChallenge.Purpose.SIGN_IN, OtpChallenge.Purpose.PASSWORD_RESET],
)
def test_account_bound_otp_request_locks_phone_mutex_before_account(
    monkeypatch, purpose
):
    account = _account()
    order = _track_shared_lock_order(monkeypatch)

    request_otp(
        PHONE,
        "203.0.113.1",
        RecordingSmsProvider(),
        purpose=purpose,
        account=account,
    )

    assert order == [OtpThrottle, Account]


@pytest.mark.django_db
def test_first_use_account_transition_locks_phone_mutex_before_account(monkeypatch):
    account = _account()
    challenge = OtpChallenge.objects.create(
        phone_hash=account.phone_hash,
        phone_encrypted=account.phone_encrypted,
        ip_hash="f" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )
    order = _track_shared_lock_order(monkeypatch)

    with pytest.raises(ExistingAccountRequiresLogin):
        create_or_upgrade_account(challenge, "Replacement strong passphrase 2026")

    assert order == [OtpThrottle, Account]


@pytest.mark.django_db
def test_reset_completion_stays_account_first_without_later_phone_mutex(monkeypatch):
    account = _account()
    challenge = OtpChallenge.objects.create(
        phone_hash=account.phone_hash,
        phone_encrypted=account.phone_encrypted,
        ip_hash="e" * 64,
        purpose=OtpChallenge.Purpose.PASSWORD_RESET,
        account=account,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )
    order = _track_shared_lock_order(monkeypatch)

    complete_verified_password_reset(
        account.pk,
        challenge.pk,
        "Replacement strong passphrase 2026",
    )

    assert order[0] is Account
    assert OtpThrottle not in order
