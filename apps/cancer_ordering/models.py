import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.labs.models import ImmutableEvent


class _RevisionAggregate(models.Model):
    revision_number = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding and set(kwargs.get('update_fields') or ()) != {'revision_number'}:
            raise ValidationError('原稿和来源不可覆盖，请追加修订。')
        return super().save(*args, **kwargs)


class CancerCandidate(_RevisionAggregate):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='cancer_candidates')
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='cancer_candidates')
    source_fact = models.ForeignKey('facts.Fact', on_delete=models.CASCADE, related_name='cancer_candidates')
    source_report = models.ForeignKey('facts.ClinicalReport', null=True, blank=True, on_delete=models.CASCADE, related_name='cancer_candidates')
    occurrence_key = models.CharField(max_length=64)
    rule_version = models.CharField(max_length=64)
    original_data = models.JSONField()
    original_source = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']
        constraints = [models.UniqueConstraint(fields=['source_fact', 'occurrence_key'], name='cancer_candidate_occurrence')]
        indexes = [models.Index(fields=['patient', 'document'], name='cancer_patient_document')]

    def clean(self):
        super().clean()
        if (self.document.patient_id != self.patient_id or self.source_fact.document_id != self.document_id
                or self.source_fact.clinical_report_id != self.source_report_id):
            raise ValidationError('候选须绑定同一患者、原件、事实与报告。')


class CandidateRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(CancerCandidate, on_delete=models.CASCADE, related_name='revisions')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=16)
    before = models.JSONField()
    after = models.JSONField()
    reason = models.TextField(blank=True, max_length=1000)
    checked_original = models.BooleanField(default=False)
    source_token = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sequence']
        constraints = [models.UniqueConstraint(fields=['candidate', 'sequence'], name='cancer_revision_sequence')]


class CollectionRun(ImmutableEvent):
    """Append-only complete/failed receipt, including a successful empty scope."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='cancer_collections')
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='cancer_collections')
    parsing_version = models.ForeignKey('processing.ParsingVersion', null=True, blank=True, on_delete=models.CASCADE)
    source_fact = models.ForeignKey('facts.Fact', null=True, blank=True, on_delete=models.CASCADE)
    scope_key = models.CharField(max_length=64)
    sequence = models.PositiveIntegerField()
    rule_version = models.CharField(max_length=64)
    input_fingerprint = models.CharField(max_length=64)
    input_snapshot = models.JSONField()
    status = models.CharField(max_length=12, choices=[('COMPLETE', '收集完成'), ('FAILED', '收集失败')])
    error_code = models.CharField(max_length=64, blank=True)
    candidate_count = models.PositiveIntegerField(default=0)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    author_snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['scope_key', 'sequence']
        constraints = [
            models.UniqueConstraint(fields=['patient', 'scope_key', 'sequence'], name='cancer_collection_sequence'),
            models.CheckConstraint(condition=(
                models.Q(parsing_version__isnull=False, source_fact__isnull=True)
                | models.Q(parsing_version__isnull=True, source_fact__isnull=False)), name='cancer_collection_scope'),
        ]

    def clean(self):
        super().clean()
        if self.document.patient_id != self.patient_id:
            raise ValidationError('收集范围须属于当前患者。')
        if self.parsing_version_id and self.parsing_version.document_id != self.document_id:
            raise ValidationError('收集范围的解析版本不属于该原件。')
        if self.source_fact_id and (self.source_fact.document_id != self.document_id or self.source_fact.origin != 'MANUAL'):
            raise ValidationError('人工范围须绑定该原件中的实际人工摘录。')


class CollectionCandidate(ImmutableEvent):
    collection = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name='members')
    candidate = models.ForeignKey(CancerCandidate, on_delete=models.CASCADE, related_name='collections')
    ordinal = models.PositiveIntegerField()

    class Meta:
        ordering = ['ordinal']
        constraints = [
            models.UniqueConstraint(fields=['collection', 'candidate'], name='cancer_collection_candidate'),
            models.UniqueConstraint(fields=['collection', 'ordinal'], name='cancer_collection_ordinal'),
        ]

    def clean(self):
        super().clean()
        if (self.collection.patient_id != self.candidate.patient_id or self.collection.document_id != self.candidate.document_id
                or (self.collection.parsing_version_id and self.candidate.source_fact.parsing_version_id != self.collection.parsing_version_id)
                or (self.collection.source_fact_id and self.candidate.source_fact_id != self.collection.source_fact_id)):
            raise ValidationError('候选须属于本次实际收集范围。')


class DisplaySelection(_RevisionAggregate):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.OneToOneField('patients.Patient', on_delete=models.CASCADE, related_name='cancer_display_selection')
    created_at = models.DateTimeField(auto_now_add=True)


class SelectionRevision(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    selection = models.ForeignKey(DisplaySelection, on_delete=models.CASCADE, related_name='revisions')
    candidate = models.ForeignKey(CancerCandidate, null=True, blank=True, on_delete=models.SET_NULL)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    sequence = models.PositiveIntegerField()
    action = models.CharField(max_length=16, choices=[('SELECT', '选择显示顺序'), ('UNDO', '撤销选择')])
    before = models.JSONField()
    after = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sequence']
        constraints = [models.UniqueConstraint(fields=['selection', 'sequence'], name='cancer_selection_sequence')]
