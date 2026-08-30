import uuid

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
    raw_value = models.CharField(max_length=256)
    result_type = models.CharField(max_length=24, choices=ResultType.choices)
    raw_unit = models.CharField(max_length=64, blank=True)
    reference_range_raw = models.CharField(max_length=512, blank=True)
    report_flag_raw = models.CharField(max_length=32, blank=True)
    observation_date = models.DateField(null=True, blank=True)
    institution_raw = models.CharField(max_length=512, blank=True)
    method_raw = models.CharField(max_length=256, blank=True)
    capability_level = models.CharField(max_length=20, choices=CapabilityLevel.choices)
    dictionary_version = models.CharField(max_length=64, validators=[_VERSION_PATTERN])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
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
        if not isinstance(self.raw_value, str) or not self.raw_value.strip():
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
