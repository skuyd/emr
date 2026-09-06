import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models


_VERSION_PATTERN = RegexValidator(
    regex=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$",
    message="Version identifiers must be stable, printable identifiers.",
)
_CODE_PATTERN = RegexValidator(
    regex=r"^[A-Z][A-Z0-9_]{2,63}$",
    message="Indicator codes must be stable uppercase identifiers.",
)


class ResultType(models.TextChoices):
    NUMERIC = "NUMERIC", "Numeric"
    COMPARATOR = "COMPARATOR", "Comparator"
    QUALITATIVE = "QUALITATIVE", "Qualitative"
    SEMI_QUANTITATIVE = "SEMI_QUANTITATIVE", "Semi-quantitative"
    STATUS = "STATUS", "Status"


class CapabilityLevel(models.TextChoices):
    STABLE = "STABLE", "Stable"
    EXPLORATORY = "EXPLORATORY", "Exploratory"
    SEARCH_ONLY = "SEARCH_ONLY", "Search only"


class LabObservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parsing_version = models.ForeignKey(
        "processing.ParsingVersion",
        on_delete=models.CASCADE,
        related_name="lab_observations",
    )
    document_page = models.ForeignKey(
        "documents.DocumentPage",
        on_delete=models.RESTRICT,
        related_name="lab_observations",
    )
    evidence = models.ForeignKey(
        "processing.SourceEvidence",
        on_delete=models.RESTRICT,
        related_name="lab_observations",
    )
    reading_order = models.PositiveIntegerField()
    raw_name = models.CharField(max_length=256)
    standard_code = models.CharField(max_length=64, validators=[_CODE_PATTERN])
    standard_name = models.CharField(max_length=160)
    raw_value = models.CharField(max_length=256, blank=True)
    result_type = models.CharField(max_length=24, choices=ResultType.choices)
    raw_unit = models.CharField(max_length=64, blank=True)
    reference_range_raw = models.CharField(max_length=512, blank=True)
    report_flag_raw = models.CharField(max_length=32, blank=True)
    observation_date = models.DateField(null=True, blank=True)
    institution_raw = models.CharField(max_length=512, blank=True)
    method_raw = models.CharField(max_length=256, blank=True)
    capability_level = models.CharField(max_length=20, choices=CapabilityLevel.choices)
    dictionary_version = models.CharField(max_length=64, validators=[_VERSION_PATTERN])
    specimen = models.CharField(max_length=32, blank=True)
    field_evidence = models.JSONField(default=dict, blank=True)
    quality_issues = models.JSONField(default=list, blank=True)
    normalization_candidates = models.JSONField(default=list, blank=True)
    reference_range = models.JSONField(default=dict, blank=True)
    quality_rule_version = models.CharField(max_length=64, blank=True)
    revision_number = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        permissions = [("review_labobservation", "Review explicitly assigned laboratory observations")]
        constraints = [
            models.UniqueConstraint(
                fields=["parsing_version", "document_page", "reading_order"],
                name="labs_observation_page_order",
            )
        ]
        indexes = [
            models.Index(fields=["parsing_version", "reading_order"], name="labs_version_order"),
            models.Index(fields=["standard_code", "observation_date"], name="labs_standard_date"),
            models.Index(fields=["raw_name"], name="labs_raw_name"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not isinstance(self.raw_name, str) or not self.raw_name.strip():
            errors["raw_name"] = "Raw indicator name must not be empty."
        if not isinstance(self.standard_name, str) or not self.standard_name.strip():
            errors["standard_name"] = "Standard indicator name must not be empty."
        missing_candidate = (
            self.raw_value == "" and self.capability_level == CapabilityLevel.SEARCH_ONLY
            and self.result_type == ResultType.STATUS and isinstance(self.quality_issues, list)
            and any(isinstance(item, dict) and item.get("code") == "association_conflict" for item in self.quality_issues)
        )
        if not isinstance(self.raw_value, str) or (not self.raw_value.strip() and not missing_candidate):
            errors["raw_value"] = "Raw result must not be empty."
        if self.parsing_version_id:
            from apps.processing.models import ParsingVersion, SourceEvidence
            from apps.documents.models import DocumentPage

            version = ParsingVersion.objects.filter(pk=self.parsing_version_id).values(
                "document_id", "dictionary_version"
            ).first()
            page_document_id = DocumentPage.objects.filter(pk=self.document_page_id).values_list(
                "document_id", flat=True
            ).first()
            if version is None or page_document_id is None or version["document_id"] != page_document_id:
                errors["document_page"] = "Observation page must belong to the parsed document."
            evidence = SourceEvidence.objects.filter(pk=self.evidence_id).values(
                "parsing_version_id", "document_page_id"
            ).first()
            if (
                evidence is None
                or evidence["parsing_version_id"] != self.parsing_version_id
                or evidence["document_page_id"] != self.document_page_id
            ):
                errors["evidence"] = "Observation evidence must belong to the same parsing version and page."
            if version is None or self.dictionary_version != version["dictionary_version"]:
                errors["dictionary_version"] = "Observation dictionary must match its parsing version."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Lab observation {self.pk}"


class RevisionAction(models.TextChoices):
    CONFIRM = "CONFIRM", "与原件一致"
    REPORT_ERROR = "REPORT_ERROR", "识别有误"
    DEFER = "DEFER", "暂不处理"
    CORRECT = "CORRECT", "更正"
    UNDO = "UNDO", "撤销上次操作"
    KEEP_REVISION = "KEEP_REVISION", "核对后沿用人工修订"
    USE_AUTOMATIC = "USE_AUTOMATIC", "核对后采用本次识别"


class ImmutableEvent(models.Model):
    """Append-only history; deletion remains governed by the document lifecycle."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("历史记录不可修改，请追加新的操作。")
        return super().save(*args, **kwargs)


class ObservationRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    observation = models.ForeignKey(LabObservation, on_delete=models.CASCADE, related_name="revisions")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    origin = models.CharField(max_length=12, choices=[("USER", "用户修订"), ("REVIEW", "复核结论")])
    action = models.CharField(max_length=16, choices=RevisionAction.choices)
    sequence = models.PositiveIntegerField()
    before = models.JSONField()
    after = models.JSONField()
    source_evidence = models.ForeignKey("processing.SourceEvidence", on_delete=models.RESTRICT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["observation", "sequence"], name="labs_revision_sequence")]


class ReviewTaskStatus(models.TextChoices):
    PENDING = "PENDING", "待分配"
    IN_PROGRESS = "IN_PROGRESS", "处理中"
    COMPLETED = "COMPLETED", "已完成"
    UNABLE = "UNABLE", "无法判断"
    REVOKED = "REVOKED", "已撤销"


class ReviewTask(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    observation = models.ForeignKey(LabObservation, on_delete=models.CASCADE, related_name="review_tasks")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="granted_lab_reviews")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_lab_reviews",
    )
    status = models.CharField(max_length=16, choices=ReviewTaskStatus.choices, default=ReviewTaskStatus.PENDING)
    revision_number = models.PositiveIntegerField(default=0)
    observation_revision = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["reviewer", "status", "created_at"], name="labs_review_queue")]


class ReviewTaskEvent(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(ReviewTask, on_delete=models.CASCADE, related_name="events")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=24)
    before_status = models.CharField(max_length=16, blank=True)
    after_status = models.CharField(max_length=16)
    revision = models.ForeignKey(ObservationRevision, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["task", "sequence"], name="labs_review_event_sequence")]


class DictionaryCandidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="dictionary_candidates")
    kind = models.CharField(max_length=12, choices=[("PROJECT", "项目/别名"), ("UNIT", "单位")])
    raw_term = models.CharField(max_length=256)
    normalized_key = models.CharField(max_length=64)
    standard_code = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=12, choices=[("PENDING", "待审核"), ("ACCEPTED", "已接受"), ("REJECTED", "已拒绝")], default="PENDING")
    definition = models.JSONField(default=dict, blank=True)
    rules = models.JSONField(default=list, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rationale = models.TextField(blank=True)
    revision_number = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["patient", "normalized_key"], name="labs_candidate_patient_key")]


class DictionaryCandidateSource(models.Model):
    candidate = models.ForeignKey(DictionaryCandidate, on_delete=models.CASCADE, related_name="sources")
    observation = models.ForeignKey(LabObservation, on_delete=models.CASCADE, related_name="dictionary_sources")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["candidate", "observation"], name="labs_candidate_source")]


class DictionaryCandidateEvent(ImmutableEvent):
    candidate = models.ForeignKey(DictionaryCandidate, on_delete=models.CASCADE, related_name="events")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    decision = models.CharField(max_length=12)
    definition = models.JSONField(default=dict)
    rules = models.JSONField(default=list)
    rationale = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["candidate", "sequence"], name="labs_candidate_event_seq")]
