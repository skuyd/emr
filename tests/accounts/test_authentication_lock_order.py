from datetime import timedelta

import pytest
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.accounts.authentication import (
    EnrollmentUnavailable,
    ExistingAccountRequiresLogin,
    complete_verified_password_reset,
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


def _track_authentication_lock_order(monkeypatch):
    order = []
    deletes = []
    original = QuerySet.select_for_update
    original_delete = QuerySet.delete

    def tracked(queryset, *args, **kwargs):
        if queryset.model in (
            AccountDeletionJob,
            OtpThrottle,
            Account,
            OtpChallenge,
        ):
            order.append(queryset.model)
        return original(queryset, *args, **kwargs)

    def tracked_delete(queryset, *args, **kwargs):
        if queryset.model in (OtpChallenge, OtpThrottle):
            deletes.append(queryset.model)
        return original_delete(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked)
    monkeypatch.setattr(QuerySet, "delete", tracked_delete)
    return order, deletes


@pytest.mark.django_db
@pytest.mark.parametrize(
    "purpose",
    [OtpChallenge.Purpose.SIGN_IN, OtpChallenge.Purpose.PASSWORD_RESET],
)
def test_account_bound_otp_request_locks_phone_mutex_before_account(
    monkeypatch, purpose
):
    account = _account()
    order, _deletes = _track_authentication_lock_order(monkeypatch)

    request_otp(
        PHONE,
        "203.0.113.1",
        RecordingSmsProvider(),
        purpose=purpose,
        account=account,
    )

    assert order[:3] == [OtpThrottle, Account, OtpChallenge]
    assert all(model is OtpChallenge for model in order[2:])


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
    order, _deletes = _track_authentication_lock_order(monkeypatch)

    with pytest.raises(ExistingAccountRequiresLogin):
        create_or_upgrade_account(challenge, "Replacement strong passphrase 2026")

    assert order == [OtpThrottle, Account, OtpChallenge]


@pytest.mark.django_db
def test_first_use_transition_handles_phone_mutex_deleted_by_concurrent_purge(
    monkeypatch,
):
    challenge = OtpChallenge.objects.create(
        phone_hash=hash_phone(CANONICAL_PHONE),
        phone_encrypted=encrypt_phone(CANONICAL_PHONE),
        ip_hash="f" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )
    original_first = QuerySet.first

    def disappearing_phone_mutex(queryset):
        if queryset.model is OtpThrottle:
            return None
        return original_first(queryset)

    monkeypatch.setattr(QuerySet, "first", disappearing_phone_mutex)

    with pytest.raises(EnrollmentUnavailable, match="^Enrollment is unavailable\\.$"):
        create_or_upgrade_account(challenge, "Replacement strong passphrase 2026")

    assert Account.objects.count() == 0


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
    order, _deletes = _track_authentication_lock_order(monkeypatch)

    complete_verified_password_reset(
        account.pk,
        challenge.pk,
        "Replacement strong passphrase 2026",
    )

    assert order[0] is Account
    assert OtpThrottle not in order


@pytest.mark.django_db
def test_account_purge_locks_phone_mutex_then_account_then_challenges_before_delete(
    monkeypatch,
):
    account = _account()
    job = AccountDeletionJob.objects.create(account=account)
    OtpThrottle.objects.create(
        scope="phone",
        identifier_hash=account.phone_hash,
    )
    OtpChallenge.objects.create(
        phone_hash=account.phone_hash,
        phone_encrypted=account.phone_encrypted,
        ip_hash="d" * 64,
        purpose=OtpChallenge.Purpose.FIRST_USE,
        otp_hash="unused",
        delivery_status=OtpChallenge.DeliveryStatus.SENT,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )
    order, deletes = _track_authentication_lock_order(monkeypatch)

    result = purge_account_deletion(job.pk)

    assert result.outcome == AccountDeletionOutcome.PURGED
    assert order == [
        AccountDeletionJob,
        OtpThrottle,
        Account,
        OtpChallenge,
    ]
    assert deletes == [OtpChallenge, OtpThrottle]
