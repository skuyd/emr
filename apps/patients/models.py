import uuid

from django.conf import settings
from django.db import models


class Patient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="patient")
    display_name = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Patient {self.pk}"


class PatientPreference(models.Model):
    patient = models.OneToOneField(Patient, on_delete=models.CASCADE, related_name="preferences")
    browser_notifications_enabled = models.BooleanField(default=False)
    browser_notification_prompted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Patient preferences {self.patient_id}"


class ProductFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="product_feedback")
    category = models.CharField(max_length=24, default="GENERAL")
    message = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["patient", "-created_at"], name="patients_feedback_recent")]

    def __str__(self):
        return f"Product feedback {self.pk}"
