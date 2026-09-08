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


class NarrativeSource(ImmutableEvent):
    """Original-position generation, independent of mutable parent reviews."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='cancer_narratives')
    document_page = models.ForeignKey('documents.DocumentPage', on_delete=models.RESTRICT)
    parsing_version = models.ForeignKey('processing.ParsingVersion', on_delete=models.CASCADE)
    source_key = models.CharField(max_length=64, unique=True)
    occurrence_key = models.CharField(max_length=64, db_index=True)
    rule_version = models.CharField(max_length=64)
    role = models.CharField(max_length=32)
    raw_text = models.TextField()
    original_data = models.JSONField()
    original_source = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        from .narrative_sources import validate_original
        validate_original(self)


class CancerCandidate(_RevisionAggregate):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE, related_name='cancer_candidates')
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE, related_name='cancer_candidates')
    source_fact = models.ForeignKey('facts.Fact', null=True, blank=True, on_delete=models.CASCADE, related_name='cancer_candidates')
    source_narrative = models.ForeignKey(NarrativeSource, null=True, blank=True, on_delete=models.CASCADE, related_name='candidates')
    source_report = models.ForeignKey('facts.ClinicalReport', null=True, blank=True, on_delete=models.CASCADE, related_name='cancer_candidates')
    occurrence_key = models.CharField(max_length=64)
    rule_version = models.CharField(max_length=64)
    original_data = models.JSONField()
    original_source = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']
        constraints = [models.UniqueConstraint(fields=['source_fact', 'occurrence_key'], name='cancer_candidate_occurrence'),
            models.CheckConstraint(condition=(models.Q(source_fact__isnull=False, source_narrative__isnull=True)
                | models.Q(source_fact__isnull=True, source_narrative__isnull=False, source_report__isnull=True)),
                name='cancer_candidate_source_xor'),
            models.UniqueConstraint(fields=['document', 'occurrence_key'], condition=models.Q(source_narrative__isnull=False),
                name='cancer_narrative_occurrence')]
        indexes = [models.Index(fields=['patient', 'document'], name='cancer_patient_document')]

    def clean(self):
        super().clean()
        if bool(self.source_fact_id) == bool(self.source_narrative_id):
            raise ValidationError('候选必须保留且仅保留一种真实来源。')
        if (self.document.patient_id != self.patient_id or (self.source_fact_id and (
                self.source_fact.document_id != self.document_id or self.source_fact.clinical_report_id != self.source_report_id))
                or (self.source_narrative_id and (self.source_narrative.document_id != self.document_id
                    or self.source_report_id is not None or self.source_narrative.occurrence_key != self.occurrence_key))):
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
    narrative_source = models.ForeignKey(NarrativeSource, null=True, blank=True, on_delete=models.CASCADE)
    ordinal = models.PositiveIntegerField()

    class Meta:
        ordering = ['ordinal']
        constraints = [
            models.UniqueConstraint(fields=['collection', 'candidate'], name='cancer_collection_candidate'),
            models.UniqueConstraint(fields=['collection', 'ordinal'], name='cancer_collection_ordinal'),
        ]

    def clean(self):
        super().clean()
        source = self.narrative_source if self.candidate.source_narrative_id else self.candidate.source_fact
        if (bool(self.narrative_source_id) != bool(self.candidate.source_narrative_id)
                or (self.narrative_source_id and self.narrative_source.occurrence_key != self.candidate.occurrence_key)):
            raise ValidationError('收集来源须与候选的原位置一致。')
        if (self.collection.patient_id != self.candidate.patient_id or self.collection.document_id != self.candidate.document_id
                or (self.collection.parsing_version_id and source.parsing_version_id != self.collection.parsing_version_id)
                or (self.collection.source_fact_id and self.candidate.source_fact_id != self.collection.source_fact_id)):
            raise ValidationError('候选须属于本次实际收集范围。')


class NarrativeDependency(ImmutableEvent):
    """Actual parent identities per collection, with deletion tombstones."""
    collection = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name='narrative_dependencies')
    narrative_source = models.ForeignKey(NarrativeSource, on_delete=models.CASCADE, related_name='dependencies')
    fact = models.ForeignKey('facts.Fact', null=True, blank=True, on_delete=models.SET_NULL, related_name='cancer_narrative_dependencies')
    original_fact_id = models.UUIDField()
    original_ranges = models.JSONField(default=list, blank=True)
    position_status = models.CharField(max_length=16)
    input_snapshot = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['collection', 'narrative_source', 'original_fact_id'],
                                              name='cancer_narrative_parent')]

    def clean(self):
        super().clean()
        source = self.narrative_source
        if (self.collection.document_id != source.document_id or self.collection.parsing_version_id != source.parsing_version_id
                or (self.fact_id and (self.fact_id != self.original_fact_id or self.fact.document_id != source.document_id
                    or self.fact.document_page_id != source.document_page_id
                    or (self.fact.origin == 'AUTOMATIC' and self.fact.parsing_version_id != source.parsing_version_id)))):
            raise ValidationError('父依赖必须保留同原件、同页及实际解析的事实身份。')


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
