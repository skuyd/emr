import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .managers import AccountManager


class Account(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_hash = models.CharField(max_length=64, unique=True)
    phone_encrypted = models.TextField()
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AccountManager()

    USERNAME_FIELD = "phone_hash"
    REQUIRED_FIELDS = ["phone_encrypted"]

    def __str__(self):
        return f"Account {self.pk}"


class OtpChallenge(models.Model):
    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    phone_hash = models.CharField(max_length=64, db_index=True)
    phone_encrypted = models.TextField()
    ip_hash = models.CharField(max_length=64, db_index=True)
    otp_hash = models.CharField(max_length=256, blank=True)
    delivery_status = models.CharField(
        max_length=8,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.READY,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["phone_hash", "created_at"]),
            models.Index(fields=["ip_hash", "created_at"]),
        ]

    def __str__(self):
        return f"OTP challenge {self.pk}"


class OtpThrottle(models.Model):
    scope = models.CharField(max_length=5)
    identifier_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scope", "identifier_hash"], name="unique_otp_throttle")
        ]


class ConsentRecord(models.Model):
    class ConsentType(models.TextChoices):
        PRIVACY = "privacy", "Privacy policy"
        SENSITIVE_DATA = "sensitive_data", "Sensitive information"
        UPLOAD_AUTHORITY = "upload_authority", "Upload authority"

    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="consent_records")
    consent_type = models.CharField(max_length=32, choices=ConsentType.choices)
    policy_version = models.CharField(max_length=32)
    policy_digest = models.CharField(max_length=64)
    granted_at = models.DateTimeField(auto_now_add=True)
    request_ip_hash = models.CharField(max_length=64)
    user_agent_hash = models.CharField(max_length=64)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "consent_type", "policy_version"],
                condition=Q(withdrawn_at__isnull=True),
                name="unique_active_consent_version",
            )
        ]
        indexes = [models.Index(fields=["account", "consent_type", "policy_version"], name="accounts_co_account_3f9d47_idx")]

    def __str__(self):
        return f"Consent record {self.pk}"


class AccountDeletionJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="deletion_job")
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["next_attempt_at", "created_at"], name="accounts_deletion_due")]

    def __str__(self):
        return f"Account deletion job {self.pk}"


class AccountSession(models.Model):
    session_key = models.CharField(max_length=40, primary_key=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="registered_sessions")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["account", "-last_seen_at"], name="accounts_session_account")]

    def __str__(self):
        return f"Account session {self.session_key[:8]}"
