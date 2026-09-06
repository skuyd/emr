import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.labs.models import ImmutableEvent


class FactCategory(models.TextChoices):
    DIAGNOSIS = "DIAGNOSIS", "诊断"
    STAGE = "STAGE", "分期"
    TREATMENT = "TREATMENT", "治疗"
    IMAGING = "IMAGING", "影像结论"
    PATHOLOGY = "PATHOLOGY", "病理结论"


class Fact(models.Model):
    """An immutable automatic candidate or an explicitly authored source excerpt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey("documents.Document", on_delete=models.CASCADE, related_name="facts")
    document_page = models.ForeignKey("documents.DocumentPage", on_delete=models.RESTRICT, related_name="facts")
    parsing_version = models.ForeignKey(
        "processing.ParsingVersion", null=True, blank=True, on_delete=models.CASCADE, related_name="facts",
    )
    evidence = models.ForeignKey("processing.SourceEvidence", null=True, blank=True, on_delete=models.RESTRICT)
    origin = models.CharField(max_length=12, choices=[("AUTOMATIC", "自动候选"), ("MANUAL", "人工补录")])
    category = models.CharField(max_length=16, choices=FactCategory.choices)
    raw_text = models.TextField()
    automatic_content = models.JSONField()
    reading_order = models.PositiveIntegerField(default=0)
    revision_number = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["document", "category"], name="facts_document_category")]

    def clean(self):
        super().clean()
        if not self.raw_text.strip() or self.document_page.document_id != self.document_id:
            raise ValidationError("事实必须绑定同一原件中的页码及原文。")
        if self.origin == "AUTOMATIC" and (not self.parsing_version_id or not self.evidence_id):
            raise ValidationError("自动候选必须保留解析版本及来源证据。")
        if self.parsing_version_id and self.parsing_version.document_id != self.document_id:
            raise ValidationError("解析版本必须属于同一原件。")
        if self.evidence_id and (
            self.evidence.parsing_version_id != self.parsing_version_id
            or self.evidence.document_page_id != self.document_page_id
            or self.evidence.source_text != self.raw_text
        ):
            raise ValidationError("来源证据与候选不匹配。")

    def save(self, *args, **kwargs):
        if not self._state.adding and set(kwargs.get("update_fields") or ()) != {"revision_number"}:
            raise ValidationError("候选和原始摘录不可覆盖，请追加修订。")
        return super().save(*args, **kwargs)


class FactRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fact = models.ForeignKey(Fact, on_delete=models.CASCADE, related_name="revisions")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=16)
    before = models.JSONField()
    after = models.JSONField()
    checked_original = models.BooleanField(default=False)
    source = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["fact", "sequence"], name="facts_revision_sequence")]


class FactExtraction(models.Model):
    parsing_version = models.OneToOneField("processing.ParsingVersion", on_delete=models.CASCADE, related_name="fact_extraction")
    status = models.CharField(max_length=20, choices=[
        ("EXTRACTED", "已提取候选"), ("NO_CANDIDATES", "未提取到明确事实"), ("FAILED", "候选提取失败"),
    ])
    extractor_version = models.CharField(max_length=40)
    candidate_count = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
