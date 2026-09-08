"""Patient-owned lesion identities and append-only, source-bound decisions."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.labs.models import ImmutableEvent


class RevisionedIdentity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision_number = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding and set(kwargs.get("update_fields") or ()) != {"revision_number"}:
            raise ValidationError("身份和原始输入不可覆盖，请追加修订。")
        return super().save(*args, **kwargs)


class Lesion(RevisionedIdentity):
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="lesions")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    original_name = models.CharField(max_length=120)

    class Meta:
        ordering = ["created_at", "pk"]


class LesionObservation(RevisionedIdentity):
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="lesion_observations")
    report = models.ForeignKey("facts.ClinicalReport", null=True, on_delete=models.SET_NULL,
                               related_name="lesion_observations")
    document = models.ForeignKey("documents.Document", null=True, on_delete=models.SET_NULL,
                                 related_name="lesion_observations")
    original_report_id = models.UUIDField()
    entity_key = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["original_report_id", "entity_key"],
                                                name="lesions_report_entity_unique")]

    def clean(self):
        super().clean()
        if self.report_id and (self.report_id != self.original_report_id
                               or self.report.document_id != self.document_id
                               or self.report.document.patient_id != self.patient_id):
            raise ValidationError("观察须绑定该患者同一份原始报告中的实体。")
        if self.document_id and self.document.patient_id != self.patient_id:
            raise ValidationError("观察原件必须属于该患者。")


class LesionOperation(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="lesion_operations")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=24)
    checked_original = models.BooleanField(default=False)
    note = models.CharField(max_length=500, blank=True)
    reverses = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="reversed_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class LesionRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesion = models.ForeignKey(Lesion, on_delete=models.CASCADE, related_name="revisions")
    operation = models.ForeignKey(LesionOperation, on_delete=models.CASCADE, related_name="lesion_revisions")
    sequence = models.PositiveIntegerField()
    before = models.JSONField()
    after = models.JSONField()

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["lesion", "sequence"], name="lesions_name_revision_unique")]


class LesionObservationRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    observation = models.ForeignKey(LesionObservation, on_delete=models.CASCADE, related_name="revisions")
    operation = models.ForeignKey(LesionOperation, on_delete=models.CASCADE, related_name="observation_revisions")
    lesion = models.ForeignKey(Lesion, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="observation_revisions")
    sequence = models.PositiveIntegerField()
    before = models.JSONField()
    after = models.JSONField()

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["observation", "sequence"],
                                                name="lesions_obs_revision_unique")]


class LesionMatchProposal(RevisionedIdentity):
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="lesion_proposals")
    first = models.ForeignKey(LesionObservation, on_delete=models.CASCADE, related_name="first_proposals")
    second = models.ForeignKey(LesionObservation, on_delete=models.CASCADE, related_name="second_proposals")
    first_binding = models.JSONField()
    second_binding = models.JSONField()
    rule_version = models.CharField(max_length=64)
    reasons = models.JSONField(default=list)
    blockers = models.JSONField(default=list)
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "fingerprint"],
                                                name="lesions_proposal_unique")]


class LesionProposalRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    proposal = models.ForeignKey(LesionMatchProposal, on_delete=models.CASCADE, related_name="revisions")
    operation = models.ForeignKey(LesionOperation, on_delete=models.CASCADE, related_name="proposal_revisions")
    sequence = models.PositiveIntegerField()
    before = models.JSONField()
    after = models.JSONField()

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["proposal", "sequence"],
                                                name="lesions_proposal_revision_unique")]
