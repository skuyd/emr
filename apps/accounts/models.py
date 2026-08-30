import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
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
        return self.phone_hash


class OtpChallenge(models.Model):
    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    phone_hash = models.CharField(max_length=64, db_index=True)
    phone_encrypted = models.TextField()
    ip_hash = models.CharField(max_length=64, db_index=True)
    otp_hash = models.CharField(max_length=256, blank=True)
    delivery_status = models.CharField(
        max_length=8,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
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
