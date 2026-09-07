"""Explicit daily-record selection and immutable snapshot dependencies."""

from copy import deepcopy

from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.urls import reverse

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.selection import identifiers
from apps.facts.readmodels import digest
from apps.patients.models import Patient

from .models import DailyRecord, DailyRecordRevision


def selected_material(patient, selection, *, lock=False):
    if not Patient.objects.filter(pk=patient.pk, deleted_at__isnull=True, account__is_active=True).exists():
        raise PermissionDenied
    ids = identifiers(selection.get('self_record_ids', []))
    query = DailyRecord.objects.filter(patient=patient, pk__in=ids).order_by('pk')
    if lock:
        query = query.select_for_update(of=('self',))
    records = list(query)
    if len(records) != len(ids):
        raise PermissionDenied
    if any(record.deleted_at is not None for record in records):
        raise ExportInputError('部分选定日常记录已删除，请重新选择。')
    revision_ids = dict(DailyRecordRevision.objects.filter(record_id__in=ids, sequence=F('record__revision_number')).values_list('record_id', 'pk'))
    rows = []
    for record in records:
        revision_id = revision_ids.get(record.pk)
        rows.append({
            'id': str(record.pk), 'kind': record.kind, 'kind_label': record.get_kind_display(), 'origin': 'USER',
            'created_by': str(record.created_by_id) if record.created_by_id else None,
            'updated_by': str(record.updated_by_id) if record.updated_by_id else None,
            'created_at': record.created_at.isoformat(), 'updated_at': record.updated_at.isoformat(),
            'revision_number': record.revision_number, 'revision_id': str(revision_id) if revision_id else None,
            'data': deepcopy(record.current_data),
            'source': {'kind': 'self_record', 'record_id': str(record.pk), 'revision_number': record.revision_number,
                       'url': reverse('self_records:detail', args=[record.pk]) + '?patient=' + str(patient.pk)},
        })
    return rows


def record_fingerprint(rows):
    return digest(rows)


def assert_records_current(patient, snapshot):
    selection = snapshot.get('selection', {})
    try:
        rows = selected_material(patient, selection, lock=True)
    except (ExportInputError, PermissionDenied, TypeError, ValueError):
        raise SnapshotChanged('选定日常记录已变化或不可用，请重新选择并生成。') from None
    expected = snapshot.get('self_record_fingerprint')
    if expected is None and not rows:
        return
    if expected != record_fingerprint(rows):
        raise SnapshotChanged('选定日常记录已有修订，请重新选择并生成。')
