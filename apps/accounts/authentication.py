from dataclasses import dataclass
from uuid import UUID

from .crypto import hash_ip, hash_phone
from .models import Account, OtpChallenge
from .phone import InvalidPhone, normalize_mainland_phone
from .services import consume_otp, enforce_password_attempt_limits, request_otp


class InvalidCredentials(Exception):
    pass


@dataclass(frozen=True)
class PendingMfa:
    account_id: UUID
    challenge_id: int


_INVALID_PHONE_THROTTLE_VALUE = "invalid-phone"
_INVALID_IP_THROTTLE_VALUE = "0.0.0.0"


def _password_value(password):
    return password if isinstance(password, str) else ""


def _run_dummy_password_hash(password):
    # Match Django's missing-user timing mitigation without persisting anything.
    Account().set_password(_password_value(password))


def _ip_hash(ip):
    try:
        return hash_ip(ip)
    except (TypeError, ValueError):
        return hash_ip(_INVALID_IP_THROTTLE_VALUE)


def begin_password_login(phone, password, ip, provider):
    ip_hash = _ip_hash(ip)
    try:
        normalized_phone = normalize_mainland_phone(phone)
    except InvalidPhone:
        _run_dummy_password_hash(password)
        enforce_password_attempt_limits(hash_phone(_INVALID_PHONE_THROTTLE_VALUE), ip_hash)
        raise InvalidCredentials("Invalid credentials") from None

    phone_hash = hash_phone(normalized_phone)
    account = Account.objects.filter(phone_hash=phone_hash).first()
    password_is_valid = False
    if account is None or not account.has_usable_password():
        _run_dummy_password_hash(password)
    else:
        password_is_valid = account.check_password(_password_value(password))

    if account is None or not account.is_active or not account.has_usable_password() or not password_is_valid:
        enforce_password_attempt_limits(phone_hash, ip_hash)
        raise InvalidCredentials("Invalid credentials")

    enforce_password_attempt_limits(phone_hash, ip_hash, succeeded=True)
    challenge = request_otp(
        normalized_phone,
        ip,
        provider,
        purpose=OtpChallenge.Purpose.SIGN_IN,
        account=account,
    )
    return PendingMfa(account_id=account.pk, challenge_id=challenge.pk)


def complete_password_login(challenge_id, code, account_id):
    challenge = consume_otp(
        challenge_id,
        code,
        purpose=OtpChallenge.Purpose.SIGN_IN,
        account_id=account_id,
    )
    return challenge.account
