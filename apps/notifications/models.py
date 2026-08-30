import re
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class NotificationKind(models.TextChoices):
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class TaskNotification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.CASCADE,
        related_name="task_notifications",
    )
    batch = models.OneToOneField(
        "documents.UploadBatch",
        on_delete=models.CASCADE,
        related_name="task_notification",
    )
    kind = models.CharField(max_length=12, choices=NotificationKind.choices)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["patient", "read_at", "-created_at"],
                name="notify_patient_unread",
            )
        ]

    def clean(self):
        super().clean()
        if self.batch_id and self.patient_id and self.batch.patient_id != self.patient_id:
            raise ValidationError({"patient": "Notification patient must match its batch"})

    def __str__(self):
        return f"Task notification {self.pk}"


class PushSubscription(models.Model):
    BROWSER_CHOICES = (
        ("chrome", "Chrome"),
        ("edge", "Edge"),
        ("safari", "Safari"),
        ("other", "Other"),
        ("unknown", "Unknown"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint_hash = models.CharField(max_length=64)
    endpoint_ciphertext = models.TextField()
    p256dh_ciphertext = models.TextField()
    auth_ciphertext = models.TextField()
    browser_family = models.CharField(max_length=12, choices=BROWSER_CHOICES, default="unknown")
    active = models.BooleanField(default=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["patient", "endpoint_hash"],
                name="notify_unique_patient_endpoint",
            )
        ]
        indexes = [
            models.Index(fields=["patient", "active"], name="notify_patient_push_active")
        ]

    def __str__(self):
        return f"Push subscription {self.pk}"


class PushDeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENDING = "SENDING", "Sending"
    RETRY = "RETRY", "Retry"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


class PushDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(
        TaskNotification,
        on_delete=models.CASCADE,
        related_name="push_deliveries",
    )
    subscription = models.ForeignKey(
        PushSubscription,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    status = models.CharField(
        max_length=8,
        choices=PushDeliveryStatus.choices,
        default=PushDeliveryStatus.PENDING,
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "subscription"],
                name="notify_unique_delivery",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="SENT", sent_at__isnull=False)
                    | Q(status__in=["PENDING", "SENDING", "RETRY", "FAILED"], sent_at__isnull=True)
                ),
                name="notify_delivery_sent_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="notify_delivery_due")
        ]

    def clean(self):
        super().clean()
        if (
            self.notification_id
            and self.subscription_id
            and self.notification.patient_id != self.subscription.patient_id
        ):
            raise ValidationError("Delivery notification and subscription must share a patient")
        if self.error_code and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.error_code) is None:
            raise ValidationError({"error_code": "Invalid stable error code"})

    def __str__(self):
        return f"Push delivery {self.pk}"

