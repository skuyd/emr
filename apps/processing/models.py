import re
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from .value_objects import InvalidRegion, normalized_polygon


_VERSION_PATTERN = RegexValidator(
    regex=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$",
    message="Version identifiers must be stable, printable identifiers.",
)
_SHA256_PATTERN = RegexValidator(regex=r"^[0-9a-f]{64}$", message="A lowercase SHA-256 digest is required.")


def validate_normalized_polygon(value):
    if value in (None, ""):
        return
    try:
        normalized_polygon(value)
    except InvalidRegion as error:
        raise ValidationError(str(error), code="invalid_polygon") from None


class ParsingVersionStatus(models.TextChoices):
    BUILDING = "BUILDING", "Building"
    READY = "READY", "Ready"
    PUBLISHED = "PUBLISHED", "Published"
    FAILED = "FAILED", "Failed"


class MetadataKind(models.TextChoices):
    DOCUMENT_TYPE = "DOCUMENT_TYPE", "Document type"
    DOCUMENT_DATE = "DOCUMENT_DATE", "Document date"
    INSTITUTION = "INSTITUTION", "Institution"


class DatePrecision(models.TextChoices):
    UNKNOWN = "UNKNOWN", "Unknown"
    YEAR = "YEAR", "Year"
    MONTH = "MONTH", "Month"
    DAY = "DAY", "Day"


class DocumentType(models.TextChoices):
    LAB = "LAB", "检验报告"
    IMAGING = "IMAGING", "影像报告"
    PATHOLOGY = "PATHOLOGY", "病理报告"
    DISCHARGE = "DISCHARGE", "出院小结"
    ORDER = "ORDER", "医嘱/处方"
    TREATMENT = "TREATMENT", "治疗记录"
    OTHER = "OTHER", "其他"
    UNKNOWN = "UNKNOWN", "未识别"


class ParsingVersionManager(models.Manager):
    def activate(self, version, *, published_at=None):
        from apps.documents.models import Document

        version_id = getattr(version, "pk", version)
        published_at = published_at or timezone.now()
        if timezone.is_naive(published_at):
            raise ValueError("Publication time must be timezone-aware")
        with transaction.atomic():
            document_id = self.values_list("document_id", flat=True).get(pk=version_id)
            document = Document.objects.select_for_update().get(pk=document_id)
            target = self.select_for_update().get(pk=version_id, document_id=document_id)
            target.document = document
            if target.status not in {ParsingVersionStatus.READY, ParsingVersionStatus.PUBLISHED}:
                raise ValueError("Only a complete parsing version can be activated")
            if target.document.deleted_at is not None:
                raise ValueError("A deleted document cannot publish parsing results")
            if target.status != ParsingVersionStatus.PUBLISHED:
                target.previous_version_id = self.filter(document_id=document_id, active=True).exclude(pk=target.pk).values_list(
                    "pk", flat=True,
                ).first()
            self.select_for_update().filter(document_id=target.document_id, active=True).exclude(pk=target.pk).update(
                active=False
            )
            target.status = ParsingVersionStatus.PUBLISHED
            target.active = True
            target.published_at = published_at
            target.save(update_fields=["status", "active", "published_at", "previous_version", "updated_at"])
        return target


class ParsingVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey("documents.Document", on_delete=models.CASCADE, related_name="parsing_versions")
    previous_version = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="successor_versions",
    )
    processing_run = models.OneToOneField(
        "documents.ProcessingRun",
        on_delete=models.RESTRICT,
        related_name="parsing_version",
    )
    parser_version = models.CharField(max_length=64, validators=[_VERSION_PATTERN])
    ocr_provider = models.CharField(max_length=64, validators=[_VERSION_PATTERN])
    ocr_provider_version = models.CharField(max_length=64, validators=[_VERSION_PATTERN])
    dictionary_version = models.CharField(max_length=64, blank=True, validators=[_VERSION_PATTERN])
    dictionary_hash = models.CharField(max_length=64, blank=True, validators=[_SHA256_PATTERN])
    status = models.CharField(
        max_length=12,
        choices=ParsingVersionStatus.choices,
        default=ParsingVersionStatus.BUILDING,
    )
    active = models.BooleanField(default=False)
    diagnostics = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ParsingVersionManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document"],
                condition=Q(active=True),
                name="processing_one_active_version",
            ),
            models.CheckConstraint(
                condition=(
                    Q(active=False)
                    | (Q(active=True) & Q(status="PUBLISHED") & Q(published_at__isnull=False))
                ),
                name="processing_active_is_published",
            ),
        ]
        indexes = [
            models.Index(fields=["document", "active"], name="processing_doc_active"),
            models.Index(fields=["status", "created_at"], name="processing_version_status"),
        ]

    def clean(self):
        super().clean()
        if self.processing_run_id and self.document_id:
            from apps.documents.models import ProcessingRun

            if not ProcessingRun.objects.filter(pk=self.processing_run_id, document_id=self.document_id).exists():
                raise ValidationError({"processing_run": "Processing run and parsing version must share a document."})
        if bool(self.dictionary_version) != bool(self.dictionary_hash):
            raise ValidationError("Dictionary version and hash must either both be set or both be empty.")
        if self.previous_version_id and (
            self.previous_version_id == self.pk
            or not ParsingVersion.objects.filter(pk=self.previous_version_id, document_id=self.document_id).exists()
        ):
            raise ValidationError({"previous_version": "A predecessor must belong to the same document."})

    def __str__(self):
        return f"Parsing version {self.pk}"


class _PageScopedModel(models.Model):
    parsing_version = models.ForeignKey(ParsingVersion, on_delete=models.CASCADE)
    document_page = models.ForeignKey("documents.DocumentPage", on_delete=models.RESTRICT)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.parsing_version_id and self.document_page_id:
            from apps.documents.models import DocumentPage

            document_id = DocumentPage.objects.filter(pk=self.document_page_id).values_list("document_id", flat=True).first()
            version_document_id = ParsingVersion.objects.filter(pk=self.parsing_version_id).values_list(
                "document_id", flat=True
            ).first()
            if document_id is None or version_document_id is None or document_id != version_document_id:
                raise ValidationError({"document_page": "Evidence page must belong to the parsed document."})


class OcrBlock(_PageScopedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parsing_version = models.ForeignKey(ParsingVersion, on_delete=models.CASCADE, related_name="ocr_blocks")
    document_page = models.ForeignKey("documents.DocumentPage", on_delete=models.RESTRICT, related_name="ocr_blocks")
    reading_order = models.PositiveIntegerField()
    text = models.TextField()
    polygon = models.JSONField(null=True, blank=True, validators=[validate_normalized_polygon])
    layout_polygon = models.JSONField(null=True, blank=True, validators=[validate_normalized_polygon])
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    provider_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parsing_version", "document_page", "reading_order"],
                name="processing_ocr_page_order",
            )
        ]
        indexes = [models.Index(fields=["parsing_version", "document_page"], name="processing_ocr_page")]

    def clean(self):
        super().clean()
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValidationError({"text": "OCR text must not be empty."})


class SourceEvidence(_PageScopedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parsing_version = models.ForeignKey(ParsingVersion, on_delete=models.CASCADE, related_name="source_evidence")
    document_page = models.ForeignKey(
        "documents.DocumentPage",
        on_delete=models.RESTRICT,
        related_name="source_evidence",
    )
    ocr_block = models.ForeignKey(OcrBlock, on_delete=models.SET_NULL, null=True, blank=True, related_name="evidence")
    polygon = models.JSONField(null=True, blank=True, validators=[validate_normalized_polygon])
    source_text = models.TextField(blank=True)
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["parsing_version", "document_page"], name="processing_evidence_page")]

    def clean(self):
        super().clean()
        if self.ocr_block_id:
            block = OcrBlock.objects.filter(pk=self.ocr_block_id).values("parsing_version_id", "document_page_id").first()
            if block is None or block["parsing_version_id"] != self.parsing_version_id or block[
                "document_page_id"
            ] != self.document_page_id:
                raise ValidationError({"ocr_block": "OCR block must belong to the same parsing version and page."})


class DocumentMetadataCandidate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parsing_version = models.ForeignKey(
        ParsingVersion,
        on_delete=models.CASCADE,
        related_name="metadata_candidates",
    )
    kind = models.CharField(max_length=24, choices=MetadataKind.choices)
    raw_text = models.TextField()
    normalized_value = models.CharField(max_length=512)
    precision = models.CharField(max_length=12, choices=DatePrecision.choices, default=DatePrecision.UNKNOWN)
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    evidence = models.ForeignKey(
        SourceEvidence,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="metadata_candidates",
    )
    selected = models.BooleanField(default=False)
    rationale = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["parsing_version", "kind", "-confidence"], name="processing_metadata_rank")]

    def clean(self):
        super().clean()
        if self.evidence_id and not SourceEvidence.objects.filter(
            pk=self.evidence_id,
            parsing_version_id=self.parsing_version_id,
        ).exists():
            raise ValidationError({"evidence": "Metadata evidence must belong to the same parsing version."})

    def __str__(self):
        return f"Metadata candidate {self.pk}"


class DocumentSummary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parsing_version = models.OneToOneField(
        ParsingVersion,
        on_delete=models.CASCADE,
        related_name="document_summary",
    )
    document_type = models.CharField(max_length=16, choices=DocumentType.choices, default=DocumentType.UNKNOWN)
    document_date_raw = models.CharField(max_length=256, blank=True)
    document_date = models.DateField(null=True, blank=True)
    date_precision = models.CharField(max_length=12, choices=DatePrecision.choices, default=DatePrecision.UNKNOWN)
    institution_raw = models.CharField(max_length=512, blank=True)
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["document_type", "-document_date"], name="processing_summary_type_date"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.document_date is None and self.date_precision != DatePrecision.UNKNOWN:
            errors["date_precision"] = "Missing dates must retain unknown precision."
        if self.document_date is not None and self.date_precision == DatePrecision.UNKNOWN:
            errors["date_precision"] = "Known dates require an explicit precision."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Document summary {self.pk}"
