"""Immutable explicit scope changes and the exact FactRevision rows affected."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.labs.models import ImmutableEvent


class LateralityScopeOperation(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey('patients.Patient', on_delete=models.CASCADE)
    document = models.ForeignKey('documents.Document', on_delete=models.CASCADE)
    action = models.CharField(max_length=12, choices=[('ADD', '补录范围'), ('REPLACE', '替换范围'),
                                                     ('ATTEST', '核对完整范围'), ('UNDO', '撤销范围操作')])
    old_fact = models.ForeignKey('facts.Fact', null=True, blank=True, on_delete=models.SET_NULL, related_name='scope_retirements')
    original_old_id = models.UUIDField(null=True, blank=True)
    new_fact = models.ForeignKey('facts.Fact', null=True, on_delete=models.SET_NULL, related_name='scope_creations')
    original_new_id = models.UUIDField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    original_author_id = models.UUIDField()
    before_state = models.JSONField()
    after_guard = models.JSONField()
    reverses = models.OneToOneField('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='reversal')
    original_reverses_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if (self.document.patient_id != self.patient_id or not self.author_id or self.author_id != self.original_author_id
                or not self.author.is_active or not self.new_fact_id or self.new_fact_id != self.original_new_id
                or self.new_fact.document_id != self.document_id or self.new_fact.field_key not in {'lesion.laterality', 'lesion.scoped_laterality'}):
            raise ValidationError('范围操作须绑定真实患者、字段和当前作者。')
        if self.old_fact_id != self.original_old_id or (self.old_fact_id and any(
                getattr(self.old_fact, key) != getattr(self.new_fact, key)
                for key in ('document_id', 'parsing_version_id', 'clinical_report_id', 'entity_key'))):
            raise ValidationError('替换前后须属于同一原报告观察，不能跨患者或实体改派来源。')
        if self.action not in {'ADD', 'REPLACE', 'ATTEST', 'UNDO'} or (self.action in {'REPLACE', 'ATTEST'} and not self.old_fact_id):
            raise ValidationError('范围操作类型与原字段不符。')
        if (self.action == 'ADD' and self.old_fact_id) or (self.action == 'UNDO') != bool(self.reverses_id):
            raise ValidationError('补录或撤销范围操作关联无效。')
        if self.reverses_id != self.original_reverses_id or (self.reverses_id and (
                self.reverses.action == 'UNDO' or self.reverses.patient_id != self.patient_id
                or self.reverses.old_fact_id != self.old_fact_id or self.reverses.new_fact_id != self.new_fact_id)):
            raise ValidationError('撤销只能指向本次原关联操作。')


class LateralityScopeOperationRevision(ImmutableEvent):
    operation = models.ForeignKey(LateralityScopeOperation, on_delete=models.CASCADE, related_name='revision_links')
    revision = models.ForeignKey('facts.FactRevision', null=True, on_delete=models.SET_NULL)
    original_revision_id = models.UUIDField()
    ordinal = models.PositiveIntegerField()

    class Meta:
        ordering = ['ordinal']
        constraints = [models.UniqueConstraint(fields=['operation', 'ordinal'], name='facts_scope_operation_order')]

    def clean(self):
        super().clean()
        if (not self.revision_id or self.revision_id != self.original_revision_id
                or self.revision.fact_id not in {self.operation.old_fact_id, self.operation.new_fact_id}
                or self.revision.author_id != self.operation.author_id):
            raise ValidationError('范围操作必须关联实际影响的字段修订和实际操作者。')
