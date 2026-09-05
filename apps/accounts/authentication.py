import ipaddress
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .crypto import hash_ip, hash_phone
from .models import Account, OtpChallenge, OtpThrottle
from .phone import InvalidPhone, normalize_mainland_phone
from .services import (
    LockedOtp,
    ThrottledPassword,
    consume_otp,
    enforce_password_attempt_limits,
    request_otp,
)
from .session_registry import revoke_account_sessions


class InvalidCredentials(Exception):
    pass


class EnrollmentUnavailable(Exception):
    pass


class ExistingAccountRequiresLogin(EnrollmentUnavailable):
    pass


class PasswordResetUnavailable(Exception):
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


def _normalized_ip_and_hash(ip):
    try:
        normalized_ip = ipaddress.ip_address(ip).compressed
    except (TypeError, ValueError):
        return None, hash_ip(_INVALID_IP_THROTTLE_VALUE)
    return normalized_ip, hash_ip(normalized_ip)


def _record_failed_attempt(phone_hash, ip_hash):
    try:
        enforce_password_attempt_limits(phone_hash, ip_hash)
    except ThrottledPassword:
        pass


def begin_password_login(phone, password, ip, provider):
    normalized_ip, ip_hash = _normalized_ip_and_hash(ip)
    try:
        normalized_phone = normalize_mainland_phone(phone)
    except InvalidPhone:
        _run_dummy_password_hash(password)
        _record_failed_attempt(hash_phone(_INVALID_PHONE_THROTTLE_VALUE), ip_hash)
        raise InvalidCredentials("Invalid credentials") from None

    phone_hash = hash_phone(normalized_phone)
    account = Account.objects.filter(phone_hash=phone_hash).first()
    if account is None or not account.has_usable_password():
        _run_dummy_password_hash(password)
    elif normalized_ip is None or not account.is_active:
        account.check_password(_password_value(password))
    if (
        normalized_ip is None
        or account is None
        or not account.is_active
        or not account.has_usable_password()
    ):
        _record_failed_attempt(phone_hash, ip_hash)
        raise InvalidCredentials("Invalid credentials")

    def authorize_account(authoritative):
        if not authoritative.has_usable_password() or not authoritative.check_password(
            _password_value(password)
        ):
            _record_failed_attempt(phone_hash, ip_hash)
            return False
        try:
            enforce_password_attempt_limits(phone_hash, ip_hash, succeeded=True)
        except ThrottledPassword:
            return False
        return True

    try:
        challenge = request_otp(
            normalized_phone,
            normalized_ip,
            provider,
            purpose=OtpChallenge.Purpose.SIGN_IN,
            account=account,
            authorize_account=authorize_account,
        )
    except LockedOtp:
        _record_failed_attempt(phone_hash, ip_hash)
        raise InvalidCredentials("Invalid credentials") from None
    if challenge is None:
        raise InvalidCredentials("Invalid credentials")
    return PendingMfa(account_id=account.pk, challenge_id=challenge.pk)


def complete_password_login(challenge_id, code, account_id):
    challenge = consume_otp(
        challenge_id,
        code,
        purpose=OtpChallenge.Purpose.SIGN_IN,
        account_id=account_id,
    )
    return challenge.account


def begin_password_reset(phone, ip, provider=None):
    normalized_phone = normalize_mainland_phone(phone)
    account = Account.objects.filter(
        phone_hash=hash_phone(normalized_phone),
        is_active=True,
    ).first()
    if account is None:
        return None
    challenge = request_otp(
        normalized_phone,
        ip,
        provider,
        purpose=OtpChallenge.Purpose.PASSWORD_RESET,
        account=account,
        defer_delivery=True,
    )
    return PendingMfa(account_id=account.pk, challenge_id=challenge.pk)


def complete_password_reset_verification(challenge_id, code, account_id):
    return consume_otp(
        challenge_id,
        code,
        purpose=OtpChallenge.Purpose.PASSWORD_RESET,
        account_id=account_id,
    )


@transaction.atomic
def reset_account_password(account, password):
    account_id = getattr(account, "pk", None)
    authoritative = Account.objects.select_for_update().filter(
        pk=account_id,
        is_active=True,
    ).first()
    if authoritative is None:
        raise PasswordResetUnavailable("Password reset is unavailable.")
    validate_password(password, user=authoritative)
    authoritative.set_password(password)
    authoritative.save(update_fields=["password", "updated_at"])
    now = timezone.now()
    outstanding_authorization_ids = list(
        OtpChallenge.objects.select_for_update()
        .filter(
            Q(purpose=OtpChallenge.Purpose.SIGN_IN, consumed_at__isnull=True)
            | Q(purpose=OtpChallenge.Purpose.PASSWORD_RESET),
            account_id=authoritative.pk,
            locked_at__isnull=True,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if outstanding_authorization_ids:
        OtpChallenge.objects.filter(pk__in=outstanding_authorization_ids).update(locked_at=now)
    revoke_account_sessions(authoritative.pk)


@transaction.atomic
def complete_verified_password_reset(account_id, challenge_id, password):
    account = Account.objects.select_for_update().filter(
        pk=account_id,
        is_active=True,
    ).first()
    if account is None:
        raise PasswordResetUnavailable("Password reset is unavailable.")
    challenge = OtpChallenge.objects.select_for_update().filter(
        pk=challenge_id,
        account_id=account_id,
        purpose=OtpChallenge.Purpose.PASSWORD_RESET,
    ).first()
    now = timezone.now()
    if (
        challenge is None
        or challenge.delivery_status
        not in (OtpChallenge.DeliveryStatus.READY, OtpChallenge.DeliveryStatus.SENT)
        or challenge.consumed_at is None
        or challenge.consumed_at > now
        or now >= challenge.consumed_at + timedelta(seconds=300)
        or challenge.locked_at is not None
    ):
        raise PasswordResetUnavailable("Password reset is unavailable.")
    reset_account_password(account, password)
    challenge.locked_at = now
    challenge.save(update_fields=["locked_at"])


def begin_first_use(phone, ip, provider):
    return request_otp(
        phone,
        ip,
        provider,
        purpose=OtpChallenge.Purpose.FIRST_USE,
    )


def complete_first_use_verification(challenge_id, code):
    return consume_otp(
        challenge_id,
        code,
        purpose=OtpChallenge.Purpose.FIRST_USE,
    )


def create_or_upgrade_account(challenge, password):
    if not isinstance(password, str) or not password:
        raise ValidationError("Password is required.")
    challenge_id = getattr(challenge, "pk", None)
    try:
        return _create_or_upgrade_account_once(challenge_id, password)
    except IntegrityError:
        # Roll back and release every first-attempt lock before inspecting a
        # concurrently created Account again in the same global order.
        return _create_or_upgrade_account_once(
            challenge_id,
            password,
            allow_create=False,
        )


@transaction.atomic
def _create_or_upgrade_account_once(challenge_id, password, *, allow_create=True):
    challenge_snapshot = OtpChallenge.objects.filter(pk=challenge_id).values(
        "phone_hash"
    ).first()
    if challenge_snapshot is None:
        raise EnrollmentUnavailable("Enrollment is unavailable.")

    # Global same-phone order: phone mutex, existing Account, then Challenge.
    phone_hash = challenge_snapshot["phone_hash"]
    OtpThrottle.objects.get_or_create(scope="phone", identifier_hash=phone_hash)
    phone_mutex = (
        OtpThrottle.objects.select_for_update()
        .filter(scope="phone", identifier_hash=phone_hash)
        .first()
    )
    if phone_mutex is None:
        raise EnrollmentUnavailable("Enrollment is unavailable.")
    account = Account.objects.select_for_update().filter(phone_hash=phone_hash).first()
    authoritative = OtpChallenge.objects.select_for_update().filter(
        pk=challenge_id
    ).first()
    now = timezone.now()
    if (
        authoritative is None
        or authoritative.phone_hash != phone_hash
        or authoritative.purpose != OtpChallenge.Purpose.FIRST_USE
        or authoritative.account_id is not None
        or authoritative.consumed_at is None
        or authoritative.consumed_at > now
        or now >= authoritative.consumed_at + timedelta(seconds=300)
        or authoritative.locked_at is not None
    ):
        raise EnrollmentUnavailable("Enrollment is unavailable.")

    if account is not None:
        return _upgrade_account(account, authoritative, password)
    if not allow_create:
        raise EnrollmentUnavailable("Enrollment is unavailable.")

    candidate = Account(
        phone_hash=authoritative.phone_hash,
        phone_encrypted=authoritative.phone_encrypted,
    )
    validate_password(password, user=candidate)
    candidate.set_password(password)
    candidate.save(force_insert=True)
    return candidate


def _upgrade_account(account, challenge, password):
    if not account.is_active:
        raise EnrollmentUnavailable("Enrollment is unavailable.")
    if account.has_usable_password():
        raise ExistingAccountRequiresLogin("Use normal login or password reset.")
    validate_password(password, user=account)
    account.phone_hash = challenge.phone_hash
    account.phone_encrypted = challenge.phone_encrypted
    account.set_password(password)
    account.save(update_fields=["phone_hash", "phone_encrypted", "password", "updated_at"])
    return account
