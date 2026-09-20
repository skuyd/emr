from dataclasses import dataclass
from functools import partial

from django.db import connection, transaction
from django.utils import timezone

from .models import BatchStatus, DocumentStatus, UploadItem, UploadItemStatus


@dataclass(frozen=True)
class BatchCounts:
    processing: int
    completed: int
    failed: int
    total: int
    accepted: int = 0
    review: int = 0
    rejected: int = 0

    @property
    def terminal(self):
        return self.total > 0 and self.processing == 0


class BatchTransactionRequired(RuntimeError):
    pass


def validity_label(validity):
    if not validity or not validity.get('units'):
        return ''
    label = f"报告已接纳 {validity.get('accepted', 0)}，待核对 {validity.get('review', 0)}，拒收 {validity.get('rejected', 0)}。"
    if validity.get('original_retained') and validity.get('rejected'):
        label += '原件已保留；当前不满足接纳条件的报告未进入有效结果。'
    elif validity.get('shared_original_retained'):
        label += '承载有效报告的完整原件已保留；无效报告未提取、未进入结果。'
    elif validity.get('status') == 'REJECTED':
        label += '缺少可确认的完整采样时间，未进入资料和结果。'
    from apps.labs.report_identity import ReportUnitEvidence

    for unit in validity['units']:
        if unit.get('reason'):
            reason = ReportUnitEvidence(unit['page_number'], 0, 0, reason=unit['reason']).reason_label
            label += f" 第 {unit['page_number']} 页报告 {unit['ordinal']}：{reason}。"
    return label


def document_validity_label(document):
    current = document.parsing_versions.filter(active=True).first()
    if current and 'report_validity' in current.diagnostics:
        from collections import Counter
        from apps.labs.report_reads import read_report_identities
        from apps.labs.reports import report_relations, relation_has_conflict

        units = tuple(current.lab_report_units.select_related('parsing_version__document', 'document_page'))
        identities = read_report_identities(document.patient, units)
        conflicts = {key for relation in report_relations(document.patient) if relation_has_conflict(relation)
                     for key in (relation.left_key, relation.right_key)} if units else set()
        projected = {}
        for unit in units:
            identity = identities[unit.pk]
            conflict = identity.status != 'REJECTED' and unit.source_key in conflicts
            projected[(unit.document_page.page_number, unit.ordinal)] = {
                'page_number': unit.document_page.page_number, 'ordinal': unit.ordinal,
                'status': 'REVIEW' if conflict else identity.status,
                'reason': 'report_identity_conflict' if conflict else identity.reason,
            }
        validity = current.diagnostics['report_validity']
        entries = [projected.get((item['page_number'], item['ordinal']), item) for item in validity['units']]
        counts = Counter(item['status'] for item in entries)
        return validity_label({**validity, 'units': entries, 'accepted': counts['ACCEPTED'],
                               'review': counts['REVIEW'], 'rejected': counts['REJECTED']})
    values = document.upload_items.filter(status='CREATED').values_list('validity', flat=True).first()
    return validity_label(values)


def item_projection_status(item):
    if item.status != UploadItemStatus.CREATED:
        return item.status
    document = getattr(item, "document", None)
    return document.status if document is not None else UploadItemStatus.UPLOADING


def summarize_batch(batch, *, items=None):
    if items is None:
        items = list(UploadItem.objects.filter(batch=batch).select_related("document").order_by("ordinal", "pk"))
    processing = completed = failed = accepted = review = rejected = 0
    for item in items:
        accepted += item.validity.get('accepted', 0)
        review += item.validity.get('review', 0)
        rejected += item.validity.get('rejected', 0)
        status = item_projection_status(item)
        if status in {UploadItemStatus.PENDING, UploadItemStatus.UPLOADING, DocumentStatus.PROCESSING}:
            processing += 1
        elif status in {UploadItemStatus.EXACT_DUPLICATE, DocumentStatus.ORGANIZED, DocumentStatus.ORIGINAL_ONLY}:
            completed += 1
        elif status in {UploadItemStatus.UPLOAD_FAILED, UploadItemStatus.REJECTED, DocumentStatus.PROCESSING_FAILED}:
            failed += 1
        else:
            processing += 1
    total = len(items)
    return BatchCounts(processing=processing, completed=completed, failed=failed, total=total,
                       accepted=accepted, review=review, rejected=rejected)


def refresh_batch_state(batch, *, now=None):
    """Persist the one authoritative ACTIVE/COMPLETED batch predicate."""

    if not connection.in_atomic_block:
        raise BatchTransactionRequired("refresh_batch_state requires transaction.atomic()")
    counts = summarize_batch(batch)
    target = BatchStatus.COMPLETED if counts.terminal else BatchStatus.ACTIVE
    changed = batch.status != target
    batch.status = target
    if target == BatchStatus.COMPLETED:
        if batch.completed_at is None:
            batch.completed_at = now or timezone.now()
            changed = True
    elif batch.completed_at is not None:
        batch.completed_at = None
        changed = True
    if changed:
        batch.save(update_fields=["status", "completed_at", "updated_at"])
    if changed and target == BatchStatus.COMPLETED:
        from apps.notifications.services import safe_create_task_notification

        transaction.on_commit(partial(safe_create_task_notification, batch.pk))
    return counts
