import uuid

from django.conf import settings
from django.db import models


class Patient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_patients")
    display_name = models.CharField(max_length=80)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Patient {self.pk}"

    @property
    def preferences(self):
        return self.preference_rows.get(account_id=self.account_id)

    def save(self, *args, **kwargs):
        from django.db import transaction
        with transaction.atomic():
            adding = self._state.adding
            super().save(*args, **kwargs)
            if adding:
                PatientMembership.objects.get_or_create(
                    patient=self, account_id=self.account_id, defaults={"role": "ADMIN"},
                )


class PatientMembership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "管理员"
        EDITOR = "EDITOR", "协作者"
        VIEWER = "VIEWER", "只读成员"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="memberships")
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="patient_memberships")
    role = models.CharField(max_length=12, choices=Role.choices)
    label = models.CharField(max_length=80, blank=True)
    revision = models.PositiveIntegerField(default=0)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "account"], name="patients_member_unique")]


class PatientDeletionJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.OneToOneField(Patient, on_delete=models.CASCADE, related_name="deletion_job")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class PatientPreference(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="preference_rows")
    account = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    browser_notifications_enabled = models.BooleanField(default=False)
    browser_notification_prompted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "account"], name="patients_preference_member_unique")]

    def save(self, *args, **kwargs):
        if not self.account_id:
            self.account_id = self.patient.account_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Patient preferences {self.patient_id}"


class ProductFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="product_feedback")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    category = models.CharField(max_length=24, default="GENERAL")
    message = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["patient", "-created_at"], name="patients_feedback_recent")]

    def __str__(self):
        return f"Product feedback {self.pk}"
