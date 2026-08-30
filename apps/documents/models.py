import unicodedata
import uuid
import re

from django.db import models
from django.db.models import F, Q


def sanitize_display_filename(value):
    """Keep only a safe, user-facing basename; object keys never use it."""
    if not isinstance(value, str):
        raise ValueError("Invalid display filename")
    basename = value.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        character for character in unicodedata.normalize("NFC", basename) if not unicodedata.category(character).startswith("C")
    ).strip()
    if not cleaned:
        raise ValueError("Invalid display filename")
    return cleaned[:255]


class DocumentStatus(models.TextChoices):
    PROCESSING = "PROCESSING", "处理中"
    ORGANIZED = "ORGANIZED", "已整理"
    ORIGINAL_ONLY = "ORIGINAL_ONLY", "仅原件"
    PROCESSING_FAILED = "PROCESSING_FAILED", "处理失败"


class UploadItemStatus(models.TextChoices):
    PENDING = "PENDING", "待上传"
    UPLOADING = "UPLOADING", "上传中"
    UPLOAD_FAILED = "UPLOAD_FAILED", "上传失败"
    CREATED = "CREATED", "处理中"
    EXACT_DUPLICATE = "EXACT_DUPLICATE", "已存在"


class BatchStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    COMPLETED = "COMPLETED", "Completed"


class ProcessingStage(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    PREPARING = "PREPARING", "Preparing"
    OCR = "OCR", "OCR"
    CLASSIFYING = "CLASSIFYING", "Classifying"
    EXTRACTING = "EXTRACTING", "Extracting"
    INDEXING = "INDEXING", "Indexing"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    NO_STRUCTURED_RESULT = "NO_STRUCTURED_RESULT", "No structured result"
    FAILED = "FAILED", "Failed"


class ImmutableDocumentFieldError(ValueError):
    pass


class InvalidDocumentMetadata(ValueError):
    pass


class PatientScopeError(ValueError):
    pass


class UploadBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="upload_batches")
    file_count = models.PositiveSmallIntegerField(default=0)
    page_count = models.PositiveSmallIntegerField(default=0)
    byte_size = models.BigIntegerField(default=0)
    status = models.CharField(max_length=12, choices=BatchStatus.choices, default=BatchStatus.ACTIVE)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(file_count__lte=20), name="documents_batch_files_max"),
            models.CheckConstraint(condition=Q(page_count__lte=60), name="documents_batch_pages_max"),
            models.CheckConstraint(condition=Q(byte_size__gte=0), name="documents_batch_bytes_nonnegative"),
            models.CheckConstraint(
                condition=(Q(status="ACTIVE", completed_at__isnull=True) | Q(status="COMPLETED", completed_at__isnull=False)),
                name="documents_batch_completion_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["patient", "-created_at"], name="documents_batch_patient_recent"),
            models.Index(fields=["patient", "status", "completed_at"], name="documents_batch_task_cards"),
        ]

    def __str__(self):
        return f"Upload batch {self.pk}"


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="documents")
    batch = models.ForeignKey(UploadBatch, on_delete=models.RESTRICT, related_name="documents")
    display_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    byte_size = models.BigIntegerField()
    page_count = models.PositiveSmallIntegerField()
    sha256 = models.CharField(max_length=64)
    perceptual_hash = models.CharField(max_length=128, blank=True)
    original_object_key = models.CharField(max_length=512, unique=True)
    status = models.CharField(max_length=24, choices=DocumentStatus.choices, default=DocumentStatus.PROCESSING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    _IMMUTABLE_FIELDS = (
        "patient_id",
        "batch_id",
        "display_filename",
        "content_type",
        "byte_size",
        "page_count",
        "sha256",
        "original_object_key",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(byte_size__gt=0), name="documents_bytes_positive"),
            models.CheckConstraint(condition=Q(page_count__gt=0), name="documents_pages_positive"),
            models.CheckConstraint(condition=~Q(original_object_key=""), name="documents_object_key_present"),
            models.CheckConstraint(
                condition=Q(purged_at__isnull=True) | (Q(deleted_at__isnull=False) & Q(purged_at__gte=F("deleted_at"))),
                name="documents_purge_after_delete",
            ),
            models.UniqueConstraint(
                fields=["patient", "sha256"], condition=Q(deleted_at__isnull=True), name="documents_active_patient_sha256"
            ),
        ]
        indexes = [
            models.Index(fields=["patient", "deleted_at", "-created_at"], name="docs_patient_active_recent"),
            models.Index(fields=["patient", "status", "-created_at"], name="docs_patient_status_recent"),
        ]

    def save(self, *args, **kwargs):
        self.display_filename = sanitize_display_filename(self.display_filename)
        if self.content_type not in {"application/pdf", "image/jpeg", "image/png", "image/heic"}:
            raise InvalidDocumentMetadata("Unsupported canonical content type")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise InvalidDocumentMetadata("Invalid SHA-256 digest")
        if (
            not self.original_object_key.startswith("originals/")
            or not self.original_object_key.removeprefix("originals/")
            or self.original_object_key.startswith("/")
            or "\\" in self.original_object_key
            or any(unicodedata.category(character).startswith("C") for character in self.original_object_key)
            or any(part in {"", ".", ".."} for part in self.original_object_key.split("/"))
        ):
            raise InvalidDocumentMetadata("Original object key must be an opaque originals path")
        if self.patient_id and self.batch_id and not UploadBatch.objects.filter(pk=self.batch_id, patient_id=self.patient_id).exists():
            raise PatientScopeError("Document patient must match its batch patient")
        if not self._state.adding:
            existing = type(self).objects.filter(pk=self.pk).values(*self._IMMUTABLE_FIELDS).first()
            if existing is not None and any(existing[field] != getattr(self, field) for field in self._IMMUTABLE_FIELDS):
                raise ImmutableDocumentFieldError("Immutable document identity changed")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Document {self.pk}"


class UploadItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="items")
    ordinal = models.PositiveSmallIntegerField()
    display_filename = models.CharField(max_length=255)
    byte_size = models.BigIntegerField(default=0)
    page_count = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=UploadItemStatus.choices, default=UploadItemStatus.PENDING)
    error_code = models.CharField(max_length=64, blank=True)
    document = models.ForeignKey(Document, on_delete=models.RESTRICT, null=True, blank=True, related_name="upload_items")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "ordinal"], name="documents_item_batch_ordinal"),
            models.CheckConstraint(condition=Q(ordinal__gt=0), name="documents_item_ordinal_positive"),
            models.CheckConstraint(condition=Q(byte_size__gte=0), name="documents_item_bytes_nonnegative"),
            models.CheckConstraint(
                condition=(
                    Q(status__in=["PENDING", "UPLOADING", "UPLOAD_FAILED"], document__isnull=True)
                    | Q(status__in=["CREATED", "EXACT_DUPLICATE"], document__isnull=False)
                ),
                name="documents_item_result_document",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status="UPLOAD_FAILED") & ~Q(error_code=""))
                    | (Q(status__in=["PENDING", "UPLOADING", "CREATED", "EXACT_DUPLICATE"]) & Q(error_code=""))
                ),
                name="documents_item_error_code_consistent",
            ),
            models.UniqueConstraint(
                fields=["document"], condition=Q(status="CREATED"), name="documents_one_created_item_document"
            ),
        ]
        indexes = [
            models.Index(fields=["batch", "status"], name="documents_item_batch_status"),
            models.Index(fields=["status", "-created_at"], name="documents_item_status_recent"),
        ]

    def save(self, *args, **kwargs):
        self.display_filename = sanitize_display_filename(self.display_filename)
        if self.error_code and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.error_code):
            raise ValueError("Invalid stable upload error code")
        if self.document_id:
            batch_patient_id = UploadBatch.objects.values_list("patient_id", flat=True).get(pk=self.batch_id)
            if not Document.objects.filter(pk=self.document_id, patient_id=batch_patient_id).exists():
                raise PatientScopeError("Upload item document must belong to its batch patient")
            if self.status == UploadItemStatus.CREATED and not Document.objects.filter(pk=self.document_id, batch_id=self.batch_id).exists():
                raise PatientScopeError("Created upload item document must belong to its batch")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Upload item {self.pk}"


class DocumentPage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="pages")
    page_number = models.PositiveSmallIntegerField()
    image_object_key = models.CharField(max_length=512, null=True, blank=True)
    text_object_key = models.CharField(max_length=512, null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    orientation = models.CharField(
        max_length=10,
        choices=(("PORTRAIT", "Portrait"), ("LANDSCAPE", "Landscape"), ("SQUARE", "Square")),
        blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["document", "page_number"], name="documents_page_document_number"),
            models.CheckConstraint(condition=Q(page_number__gt=0), name="documents_page_number_positive"),
            models.CheckConstraint(
                condition=(Q(width__isnull=True, height__isnull=True) | (Q(width__gt=0) & Q(height__gt=0))),
                name="documents_page_dimensions_valid",
            ),
        ]

    def __str__(self):
        return f"Document page {self.pk}"


class ProcessingRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="processing_runs")
    parser_version = models.CharField(max_length=64)
    task_type = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=255, unique=True)
    attempt_number = models.PositiveSmallIntegerField(default=1)
    retry_count = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    stage = models.CharField(max_length=24, choices=ProcessingStage.choices, default=ProcessingStage.QUEUED)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    error_code = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document", "attempt_number"], name="documents_run_document_attempt"
            ),
            models.UniqueConstraint(fields=["document"], condition=Q(is_current=True), name="documents_one_current_run"),
            models.CheckConstraint(
                condition=Q(is_current=False)
                | (Q(stage__in=["SUCCEEDED", "NO_STRUCTURED_RESULT"]) & Q(finished_at__isnull=False)),
                name="documents_current_run_terminal",
            ),
            models.CheckConstraint(
                condition=(
                    Q(stage__in=["SUCCEEDED", "NO_STRUCTURED_RESULT", "FAILED"], finished_at__isnull=False)
                    | Q(stage__in=["QUEUED", "PREPARING", "OCR", "CLASSIFYING", "EXTRACTING", "INDEXING"], finished_at__isnull=True)
                ),
                name="documents_run_terminal_finished",
            ),
            models.UniqueConstraint(
                fields=["document"],
                condition=Q(stage__in=["QUEUED", "PREPARING", "OCR", "CLASSIFYING", "EXTRACTING", "INDEXING"]),
                name="documents_one_nonterminal_run",
            ),
            models.CheckConstraint(condition=Q(attempt_number__gt=0), name="documents_run_attempt_positive"),
            models.CheckConstraint(
                condition=(
                    Q(stage="QUEUED", lease_token__isnull=True)
                    | Q(
                        stage__in=["PREPARING", "OCR", "CLASSIFYING", "EXTRACTING", "INDEXING"],
                        lease_token__isnull=False,
                    )
                    | Q(stage__in=["SUCCEEDED", "NO_STRUCTURED_RESULT", "FAILED"], lease_token__isnull=True)
                ),
                name="documents_run_lease_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["stage", "heartbeat_at"], name="documents_run_stale_recovery"),
            models.Index(fields=["document", "is_current"], name="documents_run_document_current"),
        ]

    def __str__(self):
        return f"Processing run {self.pk}"


class PatientUploadQuota(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.OneToOneField("patients.Patient", on_delete=models.CASCADE, related_name="upload_quota")
    batch_file_limit = models.PositiveSmallIntegerField(default=20)
    batch_page_limit = models.PositiveSmallIntegerField(default=60)
    document_limit = models.PositiveSmallIntegerField(default=300)
    page_limit = models.PositiveIntegerField(default=1000)
    storage_byte_limit = models.BigIntegerField(default=2 * 1024**3)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(batch_file_limit__gt=0), name="documents_quota_batch_files_positive"),
            models.CheckConstraint(condition=Q(batch_file_limit__lte=20), name="documents_quota_batch_files_cap"),
            models.CheckConstraint(condition=Q(batch_page_limit__gt=0), name="documents_quota_batch_pages_positive"),
            models.CheckConstraint(condition=Q(batch_page_limit__lte=60), name="documents_quota_batch_pages_cap"),
            models.CheckConstraint(condition=Q(document_limit__gt=0), name="documents_quota_documents_positive"),
            models.CheckConstraint(condition=Q(page_limit__gt=0), name="documents_quota_pages_positive"),
            models.CheckConstraint(condition=Q(storage_byte_limit__gt=0), name="documents_quota_bytes_positive"),
        ]

    def __str__(self):
        return f"Patient upload quota {self.pk}"
