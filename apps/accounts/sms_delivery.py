"""A short-lived, encrypted OTP outbox recovered by the periodic control worker."""

from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .crypto import decrypt_sms_payload, encrypt_sms_payload, hash_ip
from .models import Account, OtpChallenge, PasswordAttemptThrottle, SmsDeliveryJob
from .otp import generate_code, hash_code
from .providers import get_sms_provider


def allow_password_reset_request(ip, *, now=None):
    """Bound all reset outbox writes before distinguishing real and decoy flows."""
    now = now or timezone.now()
    try:
        ip_hash = hash_ip(ip)
    except (TypeError, ValueError):
        ip_hash = hash_ip("0.0.0.0")
    # A separate scope keeps anonymous recovery requests independent of login
    # attempts. Commit this mutex before authentication acquires account locks.
    with transaction.atomic():
        PasswordAttemptThrottle.objects.get_or_create(
            scope="reset", identifier_hash=ip_hash,
            defaults={"window_started_at": now, "attempts": 0},
        )
        throttle = PasswordAttemptThrottle.objects.select_for_update().get(
            scope="reset", identifier_hash=ip_hash,
        )
        if throttle.window_started_at <= now - timedelta(hours=1):
            throttle.window_started_at = now
            throttle.attempts = 0
        if throttle.attempts >= 30:
            return False
        throttle.attempts += 1
        throttle.save(update_fields=["window_started_at", "attempts"])
    return True


def queue_sms_delivery(challenge, phone, code, *, expires_at):
    # Commit the outbox with its challenge; the periodic scanner is the durable
    # dispatch path, so HTTP requests neither call SMS nor depend on the broker.
    return SmsDeliveryJob.objects.create(
        challenge=challenge,
        payload_encrypted=encrypt_sms_payload(phone, code, OtpChallenge.Purpose.PASSWORD_RESET),
        expires_at=expires_at,
    )


def queue_decoy_password_reset():
    code = generate_code()
    hash_code(code)
    return queue_sms_delivery(None, "+8610000000000", code, expires_at=timezone.now() + timedelta(minutes=5))


def due_sms_deliveries(*, now=None, limit=100):
    now = now or timezone.now()
    return tuple(SmsDeliveryJob.objects.filter(
        Q(expires_at__lte=now)
        | (Q(next_attempt_at__lte=now) & (Q(lease_until__isnull=True) | Q(lease_until__lte=now)))
    ).order_by("next_attempt_at", "pk").values_list("pk", flat=True)[:max(1, min(limit, 1000))])


@contextmanager
def _locked_delivery(job_id):
    snapshot = SmsDeliveryJob.objects.filter(pk=job_id).values("challenge_id", "challenge__account_id").first()
    if snapshot is None:
        yield None, None, None
        return
    with transaction.atomic():
        # Follow authentication's Account -> Challenge order, then outbox.
        account = Account.objects.select_for_update().filter(pk=snapshot["challenge__account_id"]).first()
        challenge = OtpChallenge.objects.select_for_update().filter(pk=snapshot["challenge_id"]).first()
        job = SmsDeliveryJob.objects.select_for_update().filter(pk=job_id).first()
        yield job, challenge, account


def deliver_sms_job(job_id, *, provider=None, now=None):
    now = now or timezone.now()
    token = uuid4()
    with _locked_delivery(job_id) as (job, challenge, account):
        if job is None:
            return "missing"
        if (job.expires_at <= now or challenge is None or account is None or not account.is_active
                or challenge.locked_at is not None or challenge.consumed_at is not None):
            job.delete()
            return "discarded"
        if job.next_attempt_at > now or (job.lease_until is not None and job.lease_until > now):
            return "busy"
        job.lease_token = token
        job.lease_until = now + timedelta(seconds=60)
        job.attempt_count = min(job.attempt_count + 1, 65535)
        job.save(update_fields=["lease_token", "lease_until", "attempt_count"])
        encrypted = job.payload_encrypted
        challenge.delivery_status = OtpChallenge.DeliveryStatus.READY
        challenge.save(update_fields=["delivery_status"])
    try:
        payload = decrypt_sms_payload(encrypted)
        gateway = provider if provider is not None else get_sms_provider()
        if callable(getattr(gateway, "send_otp_once", None)):
            gateway.send_otp_once(payload["phone"], payload["code"], payload["purpose"], delivery_id=str(job_id))
        else:
            gateway.send_otp(payload["phone"], payload["code"], payload["purpose"])
    except Exception:
        with _locked_delivery(job_id) as (job, _challenge, _account):
            if job is not None and job.lease_token == token:
                job.next_attempt_at = now + timedelta(seconds=30)
                job.lease_until = None
                job.lease_token = None
                job.save(update_fields=["next_attempt_at", "lease_until", "lease_token"])
        return "retry"
    with _locked_delivery(job_id) as (job, challenge, _account):
        if job is None or job.lease_token != token:
            return "superseded"
        if challenge is not None:
            challenge.delivery_status = OtpChallenge.DeliveryStatus.SENT
            challenge.save(update_fields=["delivery_status"])
        job.delete()
    return "sent"
