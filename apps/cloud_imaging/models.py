"""Private external-source proofs with immutable evidence and revision history."""

import hashlib
import math
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q

from apps.processing.models import validate_normalized_polygon


_SHA256 = re.compile(r'[a-f0-9]{64}\Z')
MAX_PAYLOAD_LENGTH = 16384


class _ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # Django's deletion collector may anonymize the author, not the proof.
        if kwargs and set(kwargs) <= {'author', 'author_id'} and all(value is None for value in kwargs.values()):
            return super().update(**kwargs)
        raise ValidationError('来源证据和历史不可覆盖，请追加新记录。')

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError('来源证据和历史不可覆盖，请追加新记录。')


class _ImmutableProof(models.Model):
    objects = _ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('来源证据和历史不可覆盖，请追加新记录。')
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{type(self).__name__} {self.pk}'


class CloudImagingScan(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'QUEUED', '等待扫描'
        RUNNING = 'RUNNING', '正在扫描'
        SUCCEEDED = 'SUCCEEDED', '扫描完成'
        FAILED = 'FAILED', '扫描未完成'
        INVALIDATED = 'INVALIDATED', '输入或访问已变化'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='cloud_imaging_scans')
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='cloud_imaging_scans')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    access_revision = models.PositiveIntegerField(default=0)
    operation_id = models.UUIDField(default=uuid.uuid4)
    input_fingerprint = models.CharField(max_length=64)
    rules_version = models.CharField(max_length=64)
    decoder_version = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    page_results = models.JSONField(default=list)
    summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['patient', 'operation_id'], name='cloud_scan_operation'),
            models.UniqueConstraint(fields=['document', 'input_fingerprint', 'rules_version', 'decoder_version'],
                                    condition=Q(status__in=['QUEUED', 'RUNNING']), name='cloud_scan_one_pending_input'),
        ]
        indexes = [models.Index(fields=['status', 'lease_expires_at'], name='cloud_scan_recovery')]

    def clean(self):
        super().clean()
        if self.document_id and self.document.patient_id != self.patient_id:
            raise ValidationError('扫描必须属于同一患者的原件。')
        if not _SHA256.fullmatch(self.input_fingerprint):
            raise ValidationError('扫描输入身份无效。')
        if self.error_code and not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', self.error_code):
            raise ValidationError('扫描结果代码无效。')

    def save(self, *args, **kwargs):
        self.clean()
        if not self._state.adding:
            keys = ('patient_id', 'document_id', 'input_fingerprint', 'rules_version', 'decoder_version', 'operation_id')
            original = type(self).objects.filter(pk=self.pk).values(*keys).first()
            if original and any(original[key] != getattr(self, key) for key in keys):
                raise ValidationError('扫描输入不可覆盖，请建立新尝试。')
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'CloudImagingScan {self.pk}'


def _valid_transform(value):
    if not isinstance(value, list) or len(value) != 3 or any(not isinstance(row, list) or len(row) != 3 for row in value):
        return False
    if any(type(v) not in (int, float) or not math.isfinite(v) for row in value for v in row):
        return False
    a, b, c = value
    determinant = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    return abs(determinant) > 1e-12


class CloudImagingEvidence(_ImmutableProof):
    class Kind(models.TextChoices):
        OCR = 'OCR', '原始文字'
        QR = 'QR', '本地二维码'
        MANUAL = 'MANUAL', '人工原页转录'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='cloud_imaging_evidence')
    document_page = models.ForeignKey('documents.DocumentPage', on_delete=models.RESTRICT, related_name='+')
    parsing_version = models.ForeignKey('processing.ParsingVersion', null=True, blank=True, on_delete=models.RESTRICT, related_name='+')
    scan = models.ForeignKey(CloudImagingScan, null=True, blank=True, on_delete=models.CASCADE, related_name='evidence')
    kind = models.CharField(max_length=8, choices=Kind.choices)
    payload = models.TextField(blank=True)
    payload_sha256 = models.CharField(max_length=64)
    payload_type = models.CharField(max_length=16, choices=[('URL', '地址候选'), ('UNSUPPORTED', '不支持的内容'), ('UNDECODED', '尚未解码')])
    reason_code = models.CharField(max_length=64, blank=True)
    document_sha256 = models.CharField(max_length=64)
    page_width = models.PositiveIntegerField(null=True, blank=True)
    page_height = models.PositiveIntegerField(null=True, blank=True)
    source_fingerprint = models.CharField(max_length=64)
    ocr_block = models.ForeignKey('processing.OcrBlock', null=True, blank=True, on_delete=models.RESTRICT, related_name='+')
    start_offset = models.PositiveIntegerField(null=True, blank=True)
    end_offset = models.PositiveIntegerField(null=True, blank=True)
    polygon = models.JSONField(null=True, blank=True)
    input_sha256 = models.CharField(max_length=64, blank=True)
    render_profile = models.CharField(max_length=100, blank=True)
    decoder_version = models.CharField(max_length=64, blank=True)
    transform = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(Q(kind='OCR', ocr_block__isnull=False, start_offset__isnull=False, end_offset__isnull=False, end_offset__gt=F('start_offset'))
                       | Q(kind__in=['QR', 'MANUAL'], ocr_block__isnull=True, start_offset__isnull=True, end_offset__isnull=True)),
            name='cloud_evidence_text_identity',
        )]

    def clean(self):
        super().clean()
        if self.payload_type not in {'URL', 'UNSUPPORTED', 'UNDECODED'} or (self.reason_code and not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', self.reason_code)):
            raise ValidationError('来源结果类型或代码无效。')
        if self.document_page.document_id != self.document_id:
            raise ValidationError('来源证据必须属于同一原件的真实页面。')
        if self.scan_id and self.scan.document_id != self.document_id:
            raise ValidationError('扫描和证据必须属于同一原件。')
        if self.parsing_version_id and self.parsing_version.document_id != self.document_id:
            raise ValidationError('解析版本和证据必须属于同一原件。')
        if (not isinstance(self.payload, str) or len(self.payload) > MAX_PAYLOAD_LENGTH
                or self.payload_sha256 != hashlib.sha256(self.payload.encode('utf-8')).hexdigest()
                or not _SHA256.fullmatch(self.source_fingerprint)
                or self.document_sha256 != self.document.sha256
                or (self.page_width, self.page_height) != (self.document_page.width, self.document_page.height)):
            raise ValidationError('原件、页面或载荷身份不一致。')
        if self.polygon is not None:
            validate_normalized_polygon(self.polygon)
        if self.kind == self.Kind.OCR:
            block = self.ocr_block
            if (not block or not self.scan_id or block.document_page_id != self.document_page_id
                    or block.parsing_version_id != self.parsing_version_id
                    or type(self.start_offset) is not int or type(self.end_offset) is not int
                    or not 0 <= self.start_offset < self.end_offset <= len(block.text)
                    or self.payload != block.text[self.start_offset:self.end_offset]
                    or self.polygon != block.polygon
                    or self.input_sha256 or self.render_profile or self.decoder_version or self.transform):
                raise ValidationError('文字来源必须保留同页原始 Unicode 区间和原始位置。')
        elif self.kind in (self.Kind.QR, self.Kind.MANUAL):
            if self.ocr_block_id or self.start_offset is not None or self.end_offset is not None:
                raise ValidationError('二维码或人工来源不能伪造 OCR 区间。')
            if self.kind == self.Kind.MANUAL:
                if self.scan_id or self.polygon is not None or self.input_sha256 or self.render_profile or self.decoder_version or self.transform:
                    raise ValidationError('人工转录只能指向真实原页，不能伪造自动解码或区域。')
            elif (not self.scan_id or not _SHA256.fullmatch(self.input_sha256) or not self.render_profile or not self.decoder_version
                  or (self.polygon is not None and not _valid_transform(self.transform))):
                raise ValidationError('二维码区域必须有可验证的本地解码输入和可逆变换。')
        else:
            raise ValidationError('不支持的来源证据类型。')


class CloudImagingSource(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', '待核对'
        CONFIRMED = 'CONFIRMED', '已核对'
        EXCLUDED = 'EXCLUDED', '已排除'
        STALE = 'STALE', '来源已变化'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='cloud_imaging_sources')
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='cloud_imaging_sources')
    evidence = models.ForeignKey(CloudImagingEvidence, on_delete=models.RESTRICT, related_name='current_sources')
    report = models.ForeignKey('facts.ClinicalReport', null=True, blank=True, on_delete=models.SET_NULL, related_name='cloud_imaging_sources')
    title = models.CharField(max_length=160, blank=True)
    current_url = models.TextField(blank=True)
    site_label = models.CharField(max_length=260, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    confirmed_fingerprint = models.CharField(max_length=64, blank=True)
    revision_number = models.PositiveIntegerField(default=0)
    operation_id = models.UUIDField(default=uuid.uuid4)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['patient', 'operation_id'], name='cloud_source_operation')]
        indexes = [models.Index(fields=['patient', 'document', 'status'], name='cloud_source_patient_document')]

    def clean(self):
        super().clean()
        if self.document.patient_id != self.patient_id or self.evidence.document_id != self.document_id:
            raise ValidationError('来源必须绑定同一患者和原件中的证据。')
        if self.report_id and (self.report.document_id != self.document_id
                               or not self.report.spans.filter(document_page_id=self.evidence.document_page_id).exists()):
            raise ValidationError('报告归属必须覆盖来源所在的同一原件页。')

    def save(self, *args, **kwargs):
        self.clean()
        if not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values('patient_id', 'document_id').first()
            if old and (old['patient_id'] != self.patient_id or old['document_id'] != self.document_id):
                raise ValidationError('来源不能改成另一份原件，请新增来源。')
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'CloudImagingSource {self.pk}'


class CloudImagingRevision(_ImmutableProof):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(CloudImagingSource, on_delete=models.CASCADE, related_name='revisions')
    evidence = models.ForeignKey(CloudImagingEvidence, on_delete=models.RESTRICT, related_name='revisions')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=20)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    source_token = models.CharField(max_length=64)
    request_digest = models.CharField(max_length=64)
    checked_original = models.BooleanField(default=False)
    operation_id = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sequence']
        constraints = [models.UniqueConstraint(fields=['source', 'sequence'], name='cloud_revision_sequence'),
                       models.UniqueConstraint(fields=['source', 'operation_id'], name='cloud_revision_operation')]

    def clean(self):
        super().clean()
        if self.evidence.document_id != self.source.document_id:
            raise ValidationError('修订必须保留同一原件的证据。')
        if not _SHA256.fullmatch(self.source_token) or not _SHA256.fullmatch(self.request_digest) or self.sequence < 1:
            raise ValidationError('修订的来源身份或序号无效。')
