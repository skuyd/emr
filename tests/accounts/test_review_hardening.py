from datetime import timedelta

import pytest
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.accounts.authentication import PasswordResetUnavailable, complete_verified_password_reset
from apps.accounts.crypto import hash_phone
from apps.accounts.models import AccountSession, OtpChallenge
from apps.accounts.session_registry import revoke_account_sessions


@pytest.mark.parametrize("password", ["1", "123456789012", "password123456"])
def test_default_password_policy_rejects_weak_passwords(password):
    with pytest.raises(ValidationError):
        validate_password(password)


@pytest.mark.django_db
def test_password_reset_invalidates_other_previously_verified_tickets(django_user_model):
    account = django_user_model.objects.create_user(
        phone_hash=hash_phone("+8613800138000"), phone_encrypted="synthetic", password="Original synthetic passphrase"
    )
    now = timezone.now()
    tickets = [OtpChallenge.objects.create(
        phone_hash=account.phone_hash, phone_encrypted="synthetic", ip_hash="a" * 64,
        purpose=OtpChallenge.Purpose.PASSWORD_RESET, account=account,
        otp_hash="synthetic", consumed_at=now, expires_at=now + timedelta(minutes=5),
    ) for _ in range(2)]
    complete_verified_password_reset(account.pk, tickets[1].pk, "New recovery passphrase 2026")

    with pytest.raises(PasswordResetUnavailable):
        complete_verified_password_reset(account.pk, tickets[0].pk, "Old ticket passphrase 2026")
    account.refresh_from_db()
    assert account.check_password("New recovery passphrase 2026")


@pytest.mark.django_db
def test_session_revocation_does_not_decode_unrelated_sessions(django_user_model, monkeypatch):
    account = django_user_model.objects.create_user(phone_hash="a" * 64, phone_encrypted="synthetic")
    unrelated = django_user_model.objects.create_user(phone_hash="b" * 64, phone_encrypted="synthetic")
    owner, other = Client(), Client()
    owner.force_login(account)
    other.force_login(unrelated)
    owner_key, other_key = owner.session.session_key, other.session.session_key

    def forbid_decode(_session):
        pytest.fail("Targeted revocation must not decode the global session table")

    monkeypatch.setattr(Session, "get_decoded", forbid_decode)
    assert revoke_account_sessions(account.pk) == 1
    assert not Session.objects.filter(session_key=owner_key).exists()
    assert Session.objects.filter(session_key=other_key).exists()


@pytest.mark.django_db
def test_explicit_legacy_session_migration_is_idempotent(django_user_model):
    account = django_user_model.objects.create_user(phone_hash="c" * 64, phone_encrypted="synthetic")
    client = Client()
    client.force_login(account)
    session_key = client.session.session_key
    AccountSession.objects.all().delete()
    call_command("register_legacy_sessions", verbosity=0)
    call_command("register_legacy_sessions", verbosity=0)
    assert AccountSession.objects.filter(account=account, session_key=session_key).count() == 1
    assert revoke_account_sessions(account.pk) == 1
    assert not Session.objects.filter(session_key=session_key).exists()
