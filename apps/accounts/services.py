import ipaddress
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .crypto import encrypt_phone, hash_ip, hash_phone
from .models import Account, OtpChallenge, OtpThrottle, PasswordAttemptThrottle
from .otp import code_matches, generate_code, hash_code
from .phone import normalize_mainland_phone


class OtpError(Exception):
    pass


class InvalidOtp(OtpError):
    pass


class LockedOtp(OtpError):
    pass


class ThrottledOtp(OtpError):
    pass


class DeliveryFailed(OtpError):
    pass


class ThrottledPassword(Exception):
    pass


def _normalize_ip(raw_ip):
    try:
        return ipaddress.ip_address(raw_ip).compressed
    except ValueError as exc:
        raise ThrottledOtp("Request cannot be processed.") from exc


def _now():
    return timezone.now()


def _cooldown_key(phone_hash):
    return f"otp:cooldown:{phone_hash}"


def _lock_otp_throttles(phone_hash, ip_hash):
    throttle_keys = (("ip", ip_hash), ("phone", phone_hash))
    for scope, identifier_hash in throttle_keys:
        OtpThrottle.objects.get_or_create(scope=scope, identifier_hash=identifier_hash)
    exact_throttle_pairs = Q(scope="ip", identifier_hash=ip_hash) | Q(
        scope="phone", identifier_hash=phone_hash
    )
    locked_rows = list(
        OtpThrottle.objects.select_for_update()
        .filter(exact_throttle_pairs)
        .order_by("scope", "identifier_hash")
    )
    locked_keys = [(row.scope, row.identifier_hash) for row in locked_rows]
    expected_keys = set(throttle_keys)
    if len(locked_keys) != len(expected_keys) or set(locked_keys) != expected_keys:
        raise LockedOtp("OTP is unavailable.")


def _enforce_durable_limits(phone_hash, ip_hash, now):
    phone_challenges = OtpChallenge.objects.filter(phone_hash=phone_hash)
    latest = phone_challenges.order_by("-created_at", "-pk").first()
    if latest is not None and latest.created_at > now - timedelta(seconds=60):
        raise ThrottledOtp("Too many requests.")
    if phone_challenges.filter(created_at__gt=now - timedelta(hours=1)).count() >= 5:
        raise ThrottledOtp("Too many requests.")
    if phone_challenges.filter(created_at__gt=now - timedelta(hours=24)).count() >= 15:
        raise ThrottledOtp("Too many requests.")
    if OtpChallenge.objects.filter(
        ip_hash=ip_hash, created_at__gt=now - timedelta(hours=1)
    ).count() >= 30:
        raise ThrottledOtp("Too many requests.")


def _purpose(value):
    try:
        return OtpChallenge.Purpose(value)
    except (TypeError, ValueError):
        raise LockedOtp("OTP is unavailable.") from None


def _active_matching_account(account, phone_hash):
    if account is None or getattr(account, "pk", None) is None:
        return None
    return (
        account.__class__.objects.select_for_update()
        .filter(pk=account.pk, phone_hash=phone_hash, is_active=True)
        .first()
    )


def request_otp(phone, ip, provider, *, purpose, account=None, authorize_account=None):
    """Issue an OTP while preserving the cross-flow database lock order.

    OTP request paths lock the durable IP/phone throttle rows before Account,
    authorize an account-bound request, inspect send limits, then lock eligible
    challenge rows.  The phone throttle is the shared same-phone mutex also
    taken before Account by the FIRST_USE transition.  Password-reset
    completion intentionally remains Account-first and never waits on this
    mutex.
    """
    if provider is None:
        raise TypeError("provider is required")
    normalized_phone = normalize_mainland_phone(phone)
    phone_hash = hash_phone(normalized_phone)
    ip_hash = hash_ip(_normalize_ip(ip))
    purpose = _purpose(purpose)
    now = _now()
    try:
        cooldown_until = cache.get(_cooldown_key(phone_hash))
    except Exception:
        cooldown_until = None
    cooldown_is_active = cooldown_until is not None and now < cooldown_until

    code = generate_code()
    with transaction.atomic():
        _lock_otp_throttles(phone_hash, ip_hash)
        account_id = None
        if purpose in (OtpChallenge.Purpose.SIGN_IN, OtpChallenge.Purpose.PASSWORD_RESET):
            active_account = _active_matching_account(account, phone_hash)
            if active_account is None:
                raise LockedOtp("OTP is unavailable.")
            if authorize_account is not None and not authorize_account(active_account):
                return None
            account_id = active_account.pk
        if cooldown_is_active:
            raise ThrottledOtp("Too many requests.")
        _enforce_durable_limits(phone_hash, ip_hash, now)
        OtpChallenge.objects.select_for_update().filter(
            phone_hash=phone_hash,
            purpose=purpose,
            account_id=account_id,
            locked_at__isnull=True,
            consumed_at__isnull=True,
            delivery_status__in=(
                OtpChallenge.DeliveryStatus.PENDING,
                OtpChallenge.DeliveryStatus.READY,
                OtpChallenge.DeliveryStatus.SENT,
            ),
        ).update(locked_at=now)
        challenge = OtpChallenge.objects.create(
            phone_hash=phone_hash,
            phone_encrypted=encrypt_phone(normalized_phone),
            ip_hash=ip_hash,
            purpose=purpose,
            account_id=account_id,
            otp_hash=hash_code(code),
            delivery_status=OtpChallenge.DeliveryStatus.READY,
            expires_at=now + timedelta(minutes=5),
        )

    try:
        provider.send_otp(normalized_phone, code, purpose)
    except Exception:
        with transaction.atomic():
            failed = OtpChallenge.objects.select_for_update().filter(pk=challenge.pk).first()
            if failed is not None:
                failed.delivery_status = OtpChallenge.DeliveryStatus.FAILED
                failed.save(update_fields=["delivery_status"])
        raise DeliveryFailed("OTP delivery could not be completed.") from None

    with transaction.atomic():
        sent = OtpChallenge.objects.select_for_update().filter(pk=challenge.pk).first()
        if sent is not None:
            sent.delivery_status = OtpChallenge.DeliveryStatus.SENT
            sent.save(update_fields=["delivery_status"])
    if sent is None:
        raise DeliveryFailed("OTP delivery could not be completed.") from None
    try:
        cache.set(_cooldown_key(phone_hash), now + timedelta(seconds=60), timeout=60)
    except Exception:
        pass
    return sent


def consume_otp(challenge_id, code, *, purpose, account_id=None):
    purpose = _purpose(purpose)
    now = _now()
    outcome_error = None
    with transaction.atomic():
        authoritative_account = None
        if purpose in (OtpChallenge.Purpose.SIGN_IN, OtpChallenge.Purpose.PASSWORD_RESET):
            authoritative_account = Account.objects.select_for_update().filter(
                pk=account_id,
                is_active=True,
            ).first()
            if authoritative_account is None:
                raise LockedOtp("OTP is unavailable.")
        challenge = OtpChallenge.objects.select_for_update().filter(pk=challenge_id).first()
        if challenge is None:
            raise LockedOtp("OTP is unavailable.")
        if challenge.purpose != purpose or challenge.account_id != account_id:
            raise LockedOtp("OTP is unavailable.")
        if purpose in (OtpChallenge.Purpose.SIGN_IN, OtpChallenge.Purpose.PASSWORD_RESET) and (
            authoritative_account is None
            or challenge.phone_hash != authoritative_account.phone_hash
        ):
            raise LockedOtp("OTP is unavailable.")
        if (
            challenge.delivery_status
            not in (OtpChallenge.DeliveryStatus.READY, OtpChallenge.DeliveryStatus.SENT)
            or challenge.consumed_at is not None
            or challenge.locked_at is not None
        ):
            raise LockedOtp("OTP is unavailable.")
        if now >= challenge.expires_at:
            challenge.locked_at = now
            challenge.save(update_fields=["locked_at"])
            outcome_error = LockedOtp
        elif not code_matches(code, challenge.otp_hash):
            challenge.attempts += 1
            update_fields = ["attempts"]
            if challenge.attempts >= 5:
                challenge.locked_at = now
                update_fields.append("locked_at")
                challenge.save(update_fields=update_fields)
                outcome_error = LockedOtp
            else:
                challenge.save(update_fields=update_fields)
                outcome_error = InvalidOtp
        else:
            challenge.consumed_at = now
            challenge.save(update_fields=["consumed_at"])
            if authoritative_account is not None:
                challenge.account = authoritative_account
            return challenge

    if outcome_error is not None:
        raise outcome_error("OTP is unavailable.")


def verify_otp(phone, code):
    """Legacy OTP-only entry point retained only for callers migrating to consume_otp."""
    raise LockedOtp("OTP is unavailable.")


def enforce_password_attempt_limits(phone_hash, ip_hash, *, succeeded=False):
    now = _now()
    limits = (("ip", ip_hash, 30), ("phone", phone_hash, 5))
    throttled = False
    with transaction.atomic():
        for scope, identifier_hash, _ in limits:
            PasswordAttemptThrottle.objects.get_or_create(
                scope=scope,
                identifier_hash=identifier_hash,
                defaults={"window_started_at": now},
            )
        pairs = Q(scope="ip", identifier_hash=ip_hash) | Q(scope="phone", identifier_hash=phone_hash)
        rows = {
            (row.scope, row.identifier_hash): row
            for row in PasswordAttemptThrottle.objects.select_for_update()
            .filter(pairs)
            .order_by("scope", "identifier_hash")
        }
        if succeeded:
            active_attempts = {
                (scope, identifier_hash): (
                    0
                    if now >= rows[(scope, identifier_hash)].window_started_at + timedelta(minutes=15)
                    else rows[(scope, identifier_hash)].attempts
                )
                for scope, identifier_hash, _ in limits
            }
            throttled = any(
                active_attempts[(scope, identifier_hash)] >= limit
                for scope, identifier_hash, limit in limits
            )
            if not throttled:
                row = rows[("phone", phone_hash)]
                row.window_started_at = now
                row.attempts = 0
                row.save(update_fields=["window_started_at", "attempts"])

        else:
            for scope, identifier_hash, limit in limits:
                row = rows[(scope, identifier_hash)]
                if now >= row.window_started_at + timedelta(minutes=15):
                    row.window_started_at = now
                    row.attempts = 0
                if row.attempts >= limit:
                    throttled = True
                else:
                    row.attempts += 1
                row.save(update_fields=["window_started_at", "attempts"])
    if throttled:
        raise ThrottledPassword("Too many password attempts.")
