import pytest

from apps.accounts.authentication import (
    InvalidCredentials,
    begin_password_login,
    complete_password_login,
)
from apps.accounts.crypto import hash_ip, hash_phone
from apps.accounts.models import Account, OtpChallenge, PasswordAttemptThrottle
from apps.accounts.services import InvalidOtp
from tests.accounts.fakes import RecordingSmsProvider


@pytest.fixture
def account_with_password(db):
    return Account.objects.create_user(
        phone_hash=hash_phone("+8613800138000"),
        phone_encrypted="ciphertext",
        password="valid-password",
    )


def test_wrong_password_does_not_issue_otp_or_authenticate(db, account_with_password):
    provider = RecordingSmsProvider()

    with pytest.raises(InvalidCredentials, match="^Invalid credentials$"):
        begin_password_login("13800138000", "wrong", "203.0.113.1", provider)

    assert provider.codes == []
    assert OtpChallenge.objects.count() == 0


def test_correct_password_requires_matching_otp(db, account_with_password):
    provider = RecordingSmsProvider()
    pending = begin_password_login(
        "13800138000", "valid-password", "203.0.113.1", provider
    )

    assert pending.account_id == account_with_password.pk
    assert PasswordAttemptThrottle.objects.get(
        scope="ip", identifier_hash=hash_ip("203.0.113.1")
    ).attempts == 0
    with pytest.raises(InvalidOtp):
        complete_password_login(pending.challenge_id, "000000", pending.account_id)
    assert complete_password_login(
        pending.challenge_id, provider.last_code, pending.account_id
    ) == account_with_password


@pytest.mark.parametrize("state", ["missing", "inactive", "unusable"])
def test_non_authenticable_accounts_have_generic_failure_without_otp(db, account_with_password, state):
    if state == "missing":
        phone = "13900139000"
    else:
        phone = "13800138000"
        if state == "inactive":
            account_with_password.is_active = False
            account_with_password.save(update_fields=["is_active"])
        else:
            account_with_password.set_unusable_password()
            account_with_password.save(update_fields=["password"])

    provider = RecordingSmsProvider()
    with pytest.raises(InvalidCredentials, match="^Invalid credentials$"):
        begin_password_login(phone, "wrong", "203.0.113.1", provider)

    assert provider.codes == []
    assert OtpChallenge.objects.count() == 0


def test_invalid_phone_records_the_ip_budget_and_stays_generic(db):
    provider = RecordingSmsProvider()
    ip_hash = hash_ip("203.0.113.1")

    with pytest.raises(InvalidCredentials, match="^Invalid credentials$"):
        begin_password_login("not-a-phone", "wrong", "203.0.113.1", provider)

    assert provider.codes == []
    assert PasswordAttemptThrottle.objects.get(scope="ip", identifier_hash=ip_hash).attempts == 1
