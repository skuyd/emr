import ipaddress
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .crypto import encrypt_phone, hash_ip, hash_phone
from .models import Account, OtpChallenge, OtpThrottle
from .otp import code_matches, generate_code, hash_code
from .phone import normalize_mainland_phone
from .providers import NullSmsProvider


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


def _normalize_ip(raw_ip):
    try:
        return ipaddress.ip_address(raw_ip).compressed
    except ValueError as exc:
        raise ThrottledOtp("Request cannot be processed.") from exc


def _now():
    return timezone.now()


def _cooldown_key(phone_hash):
    return f"otp:cooldown:{phone_hash}"


def _enforce_durable_limits(phone_hash, ip_hash, now):
    throttle_keys = (("ip", ip_hash), ("phone", phone_hash))
    for scope, identifier_hash in throttle_keys:
        OtpThrottle.objects.get_or_create(scope=scope, identifier_hash=identifier_hash)
    exact_throttle_pairs = Q(scope="ip", identifier_hash=ip_hash) | Q(
        scope="phone", identifier_hash=phone_hash
    )
    list(
        OtpThrottle.objects.select_for_update()
        .filter(exact_throttle_pairs)
        .order_by("scope", "identifier_hash")
    )

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


def request_otp(phone, ip, provider=None):
    normalized_phone = normalize_mainland_phone(phone)
    phone_hash = hash_phone(normalized_phone)
    ip_hash = hash_ip(_normalize_ip(ip))
    now = _now()
    try:
        cooldown_until = cache.get(_cooldown_key(phone_hash))
    except Exception:
        cooldown_until = None
    if cooldown_until is not None and now < cooldown_until:
        raise ThrottledOtp("Too many requests.")

    code = generate_code()
    with transaction.atomic():
        _enforce_durable_limits(phone_hash, ip_hash, now)
        challenge = OtpChallenge.objects.create(
            phone_hash=phone_hash,
            phone_encrypted=encrypt_phone(normalized_phone),
            ip_hash=ip_hash,
            otp_hash=hash_code(code),
            delivery_status=OtpChallenge.DeliveryStatus.READY,
            expires_at=now + timedelta(minutes=5),
        )

    try:
        (provider or NullSmsProvider()).send_otp(normalized_phone, code)
    except Exception:
        with transaction.atomic():
            failed = OtpChallenge.objects.select_for_update().get(pk=challenge.pk)
            failed.delivery_status = OtpChallenge.DeliveryStatus.FAILED
            failed.save(update_fields=["delivery_status"])
        raise DeliveryFailed("OTP delivery could not be completed.") from None

    with transaction.atomic():
        sent = OtpChallenge.objects.select_for_update().get(pk=challenge.pk)
        sent.delivery_status = OtpChallenge.DeliveryStatus.SENT
        sent.save(update_fields=["delivery_status"])
    try:
        cache.set(_cooldown_key(phone_hash), now + timedelta(seconds=60), timeout=60)
    except Exception:
        pass
    return sent


def verify_otp(phone, code):
    normalized_phone = normalize_mainland_phone(phone)
    phone_hash = hash_phone(normalized_phone)
    now = _now()
    outcome_error = None
    with transaction.atomic():
        challenge = (
            OtpChallenge.objects.select_for_update()
            .filter(phone_hash=phone_hash)
            .order_by("-created_at", "-pk")
            .first()
        )
        if challenge is None:
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
            account, _ = Account.objects.get_or_create(
                phone_hash=phone_hash,
                defaults={"phone_encrypted": challenge.phone_encrypted},
            )
            return account

    if outcome_error is not None:
        raise outcome_error("OTP is unavailable.")
