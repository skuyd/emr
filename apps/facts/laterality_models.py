"""Immutable scope attestations and real fragment foreign keys."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.labs.models import ImmutableEvent


class LateralityScopeBinding(ImmutableEvent):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fact = models.OneToOneField('facts.Fact', on_delete=models.CASCADE, related_name='laterality_scope_binding')
    parent_site = models.ForeignKey('facts.Fact', null=True, blank=True, on_delete=models.SET_NULL, related_name='laterality_dependants')
    original_parent_id = models.UUIDField()
    scope_kind = models.CharField(max_length=24, choices=[('NAMED_MEMBERS_ONLY', '仅限列明部位'), ('WHOLE_ENTITY', '完整部位组')])
    origin = models.CharField(max_length=12, choices=[('AUTOMATIC', '自动来源证明'), ('MANUAL', '人工核对来源')])
    rule_version = models.CharField(max_length=48)
    members = models.JSONField()
    parent_snapshot = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    original_created_by_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        parent, child = self.parent_site, self.fact
        if (not parent or parent.pk != self.original_parent_id or parent.pk == child.pk
                or parent.field_key != 'lesion.site' or parent.representation != 'FIELD'
                or child.representation != 'FIELD'
                or any(getattr(parent, key) != getattr(child, key) for key in
                       ('document_id', 'parsing_version_id', 'clinical_report_id', 'entity_key'))):
            raise ValidationError('侧别必须关联同一患者、报告、解析版本和实体的位置字段。')
        required = 'lesion.scoped_laterality' if self.scope_kind == 'NAMED_MEMBERS_ONLY' else 'lesion.laterality'
        if self.scope_kind not in {'NAMED_MEMBERS_ONLY', 'WHOLE_ENTITY'} or child.field_key != required:
            raise ValidationError('侧别范围类型与字段不匹配。')
        if (self.origin not in {'AUTOMATIC', 'MANUAL'} or not self.rule_version
                or (self.origin == 'MANUAL' and (not self.created_by_id or self.created_by_id != self.original_created_by_id))
                or (self.origin == 'AUTOMATIC' and (self.created_by_id or self.original_created_by_id))):
            raise ValidationError('人工范围证明须保留真实作者，自动来源不能伪造作者。')
        if not isinstance(self.members, list) or not self.members or not isinstance(self.parent_snapshot, dict):
            raise ValidationError('范围证明须保留原部位成员和父来源身份。')


class LateralityScopeRange(ImmutableEvent):
    binding = models.ForeignKey(LateralityScopeBinding, on_delete=models.CASCADE, related_name='ranges')
    member_key = models.CharField(max_length=20)
    ordinal = models.PositiveIntegerField()
    child_fragment = models.ForeignKey('facts.FactSourceFragment', null=True, blank=True, on_delete=models.SET_NULL, related_name='laterality_child_ranges')
    parent_fragment = models.ForeignKey('facts.FactSourceFragment', null=True, blank=True, on_delete=models.SET_NULL, related_name='laterality_parent_ranges')
    original_child_fragment_id = models.PositiveBigIntegerField()
    original_parent_fragment_id = models.PositiveBigIntegerField()
    page_id_at_creation = models.UUIDField()
    block_id_at_creation = models.UUIDField(null=True, blank=True)
    source_kind = models.CharField(max_length=16, choices=[('OCR', '原始字符范围'), ('MANUAL_PAGE', '人工原件页核对')])
    start_offset = models.PositiveIntegerField(null=True, blank=True)
    end_offset = models.PositiveIntegerField(null=True, blank=True)
    reading_order = models.PositiveIntegerField(null=True, blank=True)
    raw_text = models.TextField()
    polygon = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['ordinal']
        constraints = [models.UniqueConstraint(fields=['binding', 'ordinal'], name='facts_side_range_order')]

    def clean(self):
        super().clean()
        parent, child = self.parent_fragment, self.child_fragment
        if (not parent or not child or parent.pk != self.original_parent_fragment_id
                or child.pk != self.original_child_fragment_id or parent.fact_id != self.binding.parent_site_id
                or child.fact_id != self.binding.fact_id or parent.document_page_id != child.document_page_id
                or self.page_id_at_creation != parent.document_page_id or not self.raw_text.strip()
                or self.member_key not in {member['member_key'] for member in self.binding.members}):
            raise ValidationError('侧别原区间必须同时属于所选字段与父位置来源。')
        parent.clean()
        child.clean()
        if self.source_kind == 'OCR':
            block = parent.ocr_block
            if (not block or child.ocr_block_id != block.pk or self.block_id_at_creation != block.pk
                    or type(self.start_offset) is not int or type(self.end_offset) is not int
                    or not parent.start_offset <= self.start_offset < self.end_offset <= parent.end_offset
                    or not child.start_offset <= self.start_offset < self.end_offset <= child.end_offset
                    or self.reading_order != block.reading_order or self.polygon != block.polygon
                    or self.raw_text != block.text[self.start_offset:self.end_offset]):
                raise ValidationError('部位侧别区间须保留同一原OCR块、字符偏移与原件坐标。')
        elif (self.source_kind != 'MANUAL_PAGE' or self.binding.origin != 'MANUAL'
              or self.block_id_at_creation is not None or self.start_offset is not None or self.end_offset is not None
              or self.reading_order is not None or self.polygon is not None or child.source_kind != 'MANUAL'):
            raise ValidationError('人工原件页证明不能伪造OCR块、区间或坐标。')
