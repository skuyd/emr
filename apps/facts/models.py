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
    representation = models.CharField(max_length=8, choices=[("EXCERPT", "原文摘录"), ("FIELD", "结构化字段")], default="EXCERPT")
    clinical_report = models.ForeignKey("ClinicalReport", null=True, blank=True, on_delete=models.CASCADE, related_name="fields")
    field_key = models.CharField(max_length=80, blank=True)
    entity_key = models.CharField(max_length=100, blank=True)
    schema_version = models.CharField(max_length=40, blank=True)
    raw_text = models.TextField()
    automatic_content = models.JSONField()
    reading_order = models.PositiveIntegerField(default=0)
    revision_number = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["document", "category"], name="facts_document_category")]
        constraints = [models.CheckConstraint(
            condition=(models.Q(representation="EXCERPT", clinical_report__isnull=True, field_key="", entity_key="", schema_version="")
                       | (models.Q(representation="FIELD", clinical_report__isnull=False)
                          & ~models.Q(field_key="") & ~models.Q(entity_key="") & ~models.Q(schema_version=""))),
            name="facts_representation_identity",
        ), models.UniqueConstraint(
            fields=["clinical_report", "entity_key", "field_key"],
            condition=models.Q(representation="FIELD", field_key__in=["specimen.identity", "assay.identity", "ihc.marker"]),
            name="facts_context_anchor_unique",
        )]

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
        if self.representation == "FIELD":
            from .clinical_schema import FIELDS, validate_content

            validate_content(self.automatic_content, field_key=self.field_key)
            if self.category != FIELDS[self.field_key].category:
                raise ValidationError("字段类别与模式不一致。")
            if (not self.clinical_report_id or self.clinical_report.document_id != self.document_id
                    or self.clinical_report.parsing_version_id != self.parsing_version_id
                    or self.schema_version != self.automatic_content["schema_version"]):
                raise ValidationError("结构化字段必须属于同一报告与解析版本。")
            kind = FIELDS[self.field_key].entity_kind
            if (kind == "report" and self.entity_key != "report") or (kind != "report" and not self.entity_key.startswith(kind + ":")):
                raise ValidationError("字段实体类型不匹配。")
            if FIELDS[self.field_key].category == "PATHOLOGY" and self.clinical_report.routing_kind != "PATHOLOGY":
                raise ValidationError("病理/IHC 字段须属于明确的病理报告范围。")
        elif self.clinical_report_id or self.field_key or self.entity_key or self.schema_version:
            raise ValidationError("原文摘录不能带有结构化字段身份。")

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


class ClinicalReport(models.Model):
    """Immutable, version-local report boundary; revisions only change inclusion."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey("documents.Document", on_delete=models.CASCADE, related_name="clinical_reports")
    parsing_version = models.ForeignKey("processing.ParsingVersion", null=True, blank=True, on_delete=models.CASCADE, related_name="clinical_reports")
    origin = models.CharField(max_length=12, choices=[("AUTOMATIC", "自动分段"), ("MANUAL", "人工分段")])
    routing_kind = models.CharField(max_length=20, default="IMAGING")
    ordinal = models.PositiveIntegerField()
    title = models.CharField(max_length=256)
    segmenter_version = models.CharField(max_length=40)
    schema_version = models.CharField(max_length=40)
    source_fingerprint = models.CharField(max_length=64)
    lifecycle_revision = models.PositiveIntegerField()
    boundary_state = models.CharField(max_length=16, default="CLEAR")
    limitations = models.JSONField(default=list, blank=True)
    revision_number = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_id", "ordinal", "pk"]
        indexes = [models.Index(fields=["document", "parsing_version"], name="facts_report_version")]

    def clean(self):
        super().clean()
        if self.parsing_version_id and self.parsing_version.document_id != self.document_id:
            raise ValidationError("报告解析版本必须属于原件。")
        if self.origin == "AUTOMATIC" and not self.parsing_version_id:
            raise ValidationError("自动报告必须有解析版本。")
        if self.origin == "MANUAL" and not self.created_by_id:
            raise ValidationError("人工报告必须记录实际作者。")

    def save(self, *args, **kwargs):
        if not self._state.adding and set(kwargs.get("update_fields") or ()) != {"revision_number"}:
            raise ValidationError("报告范围不可覆盖，请创建替代报告并保留逐字段审计。")
        return super().save(*args, **kwargs)


class ClinicalReportSpan(ImmutableEvent):
    report = models.ForeignKey(ClinicalReport, on_delete=models.CASCADE, related_name="spans")
    document_page = models.ForeignKey("documents.DocumentPage", on_delete=models.RESTRICT)
    ocr_block = models.ForeignKey("processing.OcrBlock", null=True, blank=True, on_delete=models.RESTRICT)
    ordinal = models.PositiveIntegerField()
    start_offset = models.PositiveIntegerField(null=True, blank=True)
    end_offset = models.PositiveIntegerField(null=True, blank=True)
    raw_text = models.TextField(blank=True)
    boundary_basis = models.CharField(max_length=40)

    class Meta:
        ordering = ["ordinal"]
        constraints = [models.UniqueConstraint(fields=["report", "ordinal"], name="facts_report_span_order")]

    def clean(self):
        super().clean()
        if self.document_page.document_id != self.report.document_id:
            raise ValidationError("报告页范围必须属于原件。")
        if self.ocr_block_id:
            block = self.ocr_block
            if (block.parsing_version_id != self.report.parsing_version_id or block.document_page_id != self.document_page_id
                    or type(self.start_offset) is not int or type(self.end_offset) is not int
                    or not 0 <= self.start_offset < self.end_offset <= len(block.text)
                    or self.raw_text != block.text[self.start_offset:self.end_offset]):
                raise ValidationError("报告片段偏移必须绑定同页、同版本的原始 Unicode OCR。")
        elif self.start_offset is not None or self.end_offset is not None or self.report.origin != "MANUAL":
            raise ValidationError("无 OCR 的人工范围只能明确选择原件页。")


class FactSourceFragment(ImmutableEvent):
    fact = models.ForeignKey(Fact, on_delete=models.CASCADE, related_name="source_fragments")
    ordinal = models.PositiveIntegerField()
    document_page = models.ForeignKey("documents.DocumentPage", on_delete=models.RESTRICT)
    evidence = models.ForeignKey("processing.SourceEvidence", null=True, blank=True, on_delete=models.RESTRICT)
    ocr_block = models.ForeignKey("processing.OcrBlock", null=True, blank=True, on_delete=models.RESTRICT)
    source_kind = models.CharField(max_length=20, choices=[("OCR", "原始OCR片段"), ("MANUAL", "人工原件转录")])
    start_offset = models.PositiveIntegerField(null=True, blank=True)
    end_offset = models.PositiveIntegerField(null=True, blank=True)
    raw_text = models.TextField()
    polygon = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["ordinal"]
        constraints = [models.UniqueConstraint(fields=["fact", "ordinal"], name="facts_fragment_order")]

    def clean(self):
        super().clean()
        if self.fact.representation != "FIELD" or self.document_page.document_id != self.fact.document_id or not self.raw_text.strip():
            raise ValidationError("字段片段须保留同一原件中的原文。")
        spans = self.fact.clinical_report.spans.filter(document_page=self.document_page)
        if self.source_kind == "OCR":
            block = self.ocr_block
            if (not block or not self.evidence_id or self.evidence.ocr_block_id != self.ocr_block_id
                    or self.evidence.polygon != self.polygon or block.parsing_version_id != self.fact.parsing_version_id
                    or block.document_page_id != self.document_page_id
                    or type(self.start_offset) is not int or type(self.end_offset) is not int
                    or not 0 <= self.start_offset < self.end_offset <= len(block.text)
                    or self.raw_text != block.text[self.start_offset:self.end_offset]
                    or self.polygon != block.polygon
                    or not spans.filter(ocr_block=block, start_offset__lte=self.start_offset, end_offset__gte=self.end_offset).exists()):
                raise ValidationError("字段片段须位于报告边界内，坐标及字符偏移必须来自原始OCR。")
        elif (self.source_kind != "MANUAL" or self.evidence_id or self.ocr_block_id or self.start_offset is not None or self.end_offset is not None
              or not spans.exists() or self.polygon is not None):
            raise ValidationError("人工转录需明确原件页，不能伪造OCR坐标。")
        if self.evidence_id and (self.evidence.parsing_version_id != self.fact.parsing_version_id
                                 or self.evidence.document_page_id != self.document_page_id
                                 or self.evidence.source_text != self.raw_text):
            raise ValidationError("片段证据与转录不一致。")


class ClinicalReportRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(ClinicalReport, on_delete=models.CASCADE, related_name="revisions")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=20)
    before = models.JSONField()
    after = models.JSONField()
    field_revisions = models.JSONField(default=list)
    source_token = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["report", "sequence"], name="facts_report_revision_order")]


class ClinicalExtraction(models.Model):
    parsing_version = models.OneToOneField("processing.ParsingVersion", on_delete=models.CASCADE, related_name="clinical_extraction")
    extractor_version = models.CharField(max_length=40)
    schema_version = models.CharField(max_length=40)
    status = models.CharField(max_length=20)
    report_count = models.PositiveIntegerField(default=0)
    field_count = models.PositiveIntegerField(default=0)
    unparsed_page_count = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=64, blank=True)
    limitations = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


# The scope models use actual Fact/fragment foreign keys without changing old
# immutable field rows or their individual schema identities.
from .laterality_models import LateralityScopeBinding, LateralityScopeRange  # noqa: E402,F401
from .laterality_operation_models import LateralityScopeOperation, LateralityScopeOperationRevision  # noqa: E402,F401
