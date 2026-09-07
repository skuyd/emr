"""Source baselines and append-only decisions for patient-owned treatment records."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.labs.models import ImmutableEvent


class TreatmentOrigin(models.TextChoices):
    AUTOMATIC = "AUTOMATIC", "原文自动提议"
    USER = "USER", "本人补记"


class TreatmentStatus(models.TextChoices):
    PENDING = "PENDING", "尚待确认"
    CONFIRMED = "CONFIRMED", "已确认"
    REJECTED = "REJECTED", "已拒绝"
    SUPERSEDED = "SUPERSEDED", "已被替代"


class TreatmentRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE)
    origin = models.CharField(max_length=12, choices=TreatmentOrigin.choices)
    source_key = models.CharField(max_length=80)
    initial_content = models.JSONField()
    current_content = models.JSONField()
    revision_number = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            mutable = {"revision_number", "current_content"}
            if isinstance(self, TreatmentCycle):
                mutable.add("regimen")
            fields = set(kwargs.get("update_fields") or ())
            if not fields or not fields <= mutable:
                raise ValidationError("原始提议及来源身份不可覆盖，请追加更正记录。")
        return super().save(*args, **kwargs)


class TreatmentEvent(TreatmentRecord):
    rule_version = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "source_key"], name="treatment_event_source_unique")]
        indexes = [models.Index(fields=["patient", "created_at"], name="treatment_event_patient")]


class TreatmentRegimen(TreatmentRecord):
    normalized_key = models.CharField(max_length=64)
    episode_key = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "source_key"], name="treatment_regimen_src_unique")]


class TreatmentDerivationRun(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE)
    rule_version = models.CharField(max_length=64)
    input_fingerprint = models.CharField(max_length=64)
    source_manifest_hash = models.CharField(max_length=64)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    access_revision = models.PositiveIntegerField()
    result_counts = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "rule_version", "input_fingerprint"],
                                               name="treatment_run_input_unique")]


class TreatmentCycle(TreatmentRecord):
    regimen = models.ForeignKey(TreatmentRegimen, null=True, blank=True, on_delete=models.CASCADE, related_name="cycles")
    derivation_run = models.ForeignKey(TreatmentDerivationRun, null=True, blank=True, on_delete=models.CASCADE, related_name="cycles")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "source_key"], name="treatment_cycle_source_unique")]

    def clean(self):
        super().clean()
        if ((self.regimen_id and self.regimen.patient_id != self.patient_id)
                or (self.derivation_run_id and self.derivation_run.patient_id != self.patient_id)):
            raise ValidationError("周期、方案及提议批次必须属于同一患者。")


class TreatmentEvidence(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(TreatmentEvent, on_delete=models.CASCADE, related_name="evidence")
    document = models.ForeignKey("documents.Document", on_delete=models.CASCADE, related_name="treatment_evidence")
    document_page = models.ForeignKey("documents.DocumentPage", on_delete=models.CASCADE)
    parsing_version = models.ForeignKey("processing.ParsingVersion", null=True, blank=True, on_delete=models.CASCADE)
    fact = models.ForeignKey("facts.Fact", null=True, blank=True, on_delete=models.CASCADE)
    source_evidence = models.ForeignKey("processing.SourceEvidence", null=True, blank=True, on_delete=models.CASCADE)
    source_token = models.CharField(max_length=64)
    source_revision = models.PositiveIntegerField(default=0)
    lifecycle_revision = models.PositiveIntegerField(default=0)
    material_revision = models.PositiveIntegerField(default=0)
    start_offset = models.PositiveIntegerField()
    end_offset = models.PositiveIntegerField()
    raw_text = models.TextField()
    source = models.JSONField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(end_offset__gt=models.F("start_offset")), name="treatment_evidence_span"),
            models.CheckConstraint(condition=Q(fact__isnull=False) | Q(source_evidence__isnull=False),
                                   name="treatment_evidence_source"),
        ]

    def clean(self):
        super().clean()
        if (self.document.patient_id != self.event.patient_id
                or self.document_page.document_id != self.document_id
                or (self.parsing_version_id and self.parsing_version.document_id != self.document_id)
                or (self.fact_id and (self.fact.document_id != self.document_id or self.fact.document_page_id != self.document_page_id))
                or (self.source_evidence_id and (self.source_evidence.parsing_version_id != self.parsing_version_id
                                                or self.source_evidence.document_page_id != self.document_page_id))):
            raise ValidationError("治疗来源必须指向同一患者的原件、页和解析版本。")
        text = self.fact.raw_text if self.fact_id else self.source_evidence.source_text if self.source_evidence_id else ""
        if not self.raw_text or text[self.start_offset:self.end_offset] != self.raw_text:
            raise ValidationError("治疗引用必须精确对应原始文字范围。")


class CycleEventLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cycle = models.ForeignKey(TreatmentCycle, on_delete=models.CASCADE, related_name="event_links")
    event = models.ForeignKey(TreatmentEvent, on_delete=models.CASCADE, related_name="cycle_links")
    role = models.CharField(max_length=16)
    source_token = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["cycle", "event", "role"], name="treatment_cycle_event_unique")]

    def clean(self):
        super().clean()
        if self.cycle.patient_id != self.event.patient_id:
            raise ValidationError("周期和治疗事件必须属于同一患者。")


class CycleRecordLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cycle = models.ForeignKey(TreatmentCycle, on_delete=models.CASCADE, related_name="record_links")
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.CASCADE)
    observation = models.ForeignKey("labs.LabObservation", null=True, blank=True, on_delete=models.CASCADE)
    origin = models.CharField(max_length=12, choices=TreatmentOrigin.choices)
    source_token = models.CharField(max_length=64)
    assigned = models.BooleanField(default=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=Q(document__isnull=False, observation__isnull=True) | Q(document__isnull=True, observation__isnull=False),
            name="treatment_record_one_source")]

    def clean(self):
        super().clean()
        patient = (self.document.patient_id if self.document_id
                   else self.observation.parsing_version.document.patient_id if self.observation_id else None)
        if patient != self.cycle.patient_id:
            raise ValidationError("检查关联必须属于同一患者。")


class TreatmentRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE)
    event = models.ForeignKey(TreatmentEvent, null=True, blank=True, on_delete=models.CASCADE, related_name="revisions")
    regimen = models.ForeignKey(TreatmentRegimen, null=True, blank=True, on_delete=models.CASCADE, related_name="revisions")
    cycle = models.ForeignKey(TreatmentCycle, null=True, blank=True, on_delete=models.CASCADE, related_name="revisions")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    operation_id = models.UUIDField()
    request_digest = models.CharField(max_length=64)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=16)
    before = models.JSONField()
    after = models.JSONField()
    checked_original = models.BooleanField(default=False)
    source_tokens = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence", "pk"]
        constraints = [
            models.CheckConstraint(condition=(Q(event__isnull=False, regimen__isnull=True, cycle__isnull=True)
                                               | Q(event__isnull=True, regimen__isnull=False, cycle__isnull=True)
                                               | Q(event__isnull=True, regimen__isnull=True, cycle__isnull=False)),
                                   name="treatment_revision_one_target"),
            *[models.UniqueConstraint(fields=[target, "sequence"], name=f"treatment_rev_{target}_seq")
              for target in ("event", "regimen", "cycle")],
        ]
        indexes = [models.Index(fields=["patient", "operation_id"], name="treatment_revision_operation")]

    def clean(self):
        super().clean()
        targets = [target for target in (self.event, self.regimen, self.cycle) if target is not None]
        if len(targets) != 1 or targets[0].patient_id != self.patient_id:
            raise ValidationError("更正记录必须绑定同一患者的一项治疗、方案或周期。")


class CycleLineage(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    predecessor = models.ForeignKey(TreatmentCycle, on_delete=models.CASCADE, related_name="successors")
    successor = models.ForeignKey(TreatmentCycle, on_delete=models.CASCADE, related_name="predecessors")
    operation_id = models.UUIDField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["predecessor", "successor"], name="treatment_lineage_unique"),
                       models.CheckConstraint(condition=~Q(predecessor=models.F("successor")), name="treatment_lineage_not_self")]

    def clean(self):
        super().clean()
        if self.predecessor.patient_id != self.successor.patient_id:
            raise ValidationError("合并拆分前后周期必须属于同一患者。")
