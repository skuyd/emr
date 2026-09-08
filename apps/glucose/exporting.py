"""Selected glucose values and their actual document dependencies, without whole OCR bodies."""

from copy import deepcopy

from django.core.exceptions import PermissionDenied
from django.db.models import F

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.selection import identifiers
from apps.facts.readmodels import digest
from apps.patients.models import Patient

from .models import GlucoseRecord, GlucoseRevision
from .sources import source_current


DATA_KEYS = frozenset({
    'schema_version', 'source_kind', 'time_slot', 'measured_at', 'measured_date', 'local_time',
    'measured_local_raw', 'time_precision', 'timezone', 'timezone_origin', 'utc_offset',
    'raw_value', 'raw_unit', 'result_type', 'normalized_value', 'normalized_unit', 'conversion',
    'unplottable_reason', 'plot_eligible', 'source_label', 'notes', 'field_origins',
})
TIME_KEYS = frozenset({'local', 'precision', 'timezone', 'timezone_origin', 'raw', 'raw_label', 'role', 'utc_datetime'})
FIELD_KEYS = frozenset({'raw_name', 'raw_value', 'raw_unit', 'specimen', 'standard_code', 'observation_date'})
FIELD_SOURCE_KEYS = frozenset({
    'observation_id', 'evidence_id', 'revision_id', 'document_id', 'parsing_version_id',
    'page_id', 'page_number', 'observation_revision', 'effective_value',
})


def _public_data(data):
    return {key: deepcopy(value) for key, value in data.items() if key in DATA_KEYS}


def _records(patient, selection):
    if not Patient.objects.filter(pk=patient.pk, deleted_at__isnull=True, account__is_active=True).exists():
        raise PermissionDenied
    ids = identifiers(selection.get('glucose_record_ids', []))
    query = GlucoseRecord.objects.filter(patient=patient, pk__in=ids).order_by('pk')
    if query.count() != len(ids):
        raise PermissionDenied
    return ids, query


def document_dependencies(patient, selection):
    """Read immutable parent IDs before any selected child record is locked."""
    _, query = _records(patient, selection)
    return sorted({str(identity) for identity in query.values_list('source_document_id', flat=True) if identity})


def _source(record):
    data = record.current_data
    source = data.get('source', {})
    context = source.get('report_context', {})
    return {
        'record_id': str(record.pk), 'source_kind': record.source_kind,
        'document_id': str(record.source_document_id) if record.source_document_id else None,
        'page_id': str(record.source_page_id) if record.source_page_id else None,
        'page_number': source.get('page_number'),
        'parsing_version_id': str(record.source_parsing_version_id) if record.source_parsing_version_id else None,
        'observation_id': str(record.source_observation_id) if record.source_observation_id else None,
        'source_fingerprint': record.source_fingerprint,
        'specimen_raw': context.get('specimen_raw', ''),
        'sampling': {key: deepcopy(value) for key, value in context.get('sample_time', {}).items() if key in TIME_KEYS},
        'reporting': {key: deepcopy(value) for key, value in context.get('report_time', {}).items() if key in TIME_KEYS},
        'field_sources': {field: {
                              **{key: deepcopy(value) for key, value in original.items() if key in FIELD_SOURCE_KEYS},
                              'field_evidence': {key: deepcopy(value) for key, value in original.get('field_evidence', {}).items()
                                                 if key in {'page_number', 'polygon', 'precision'}},
                          }
                          for field, original in source.get('field_sources', {}).items() if field in FIELD_KEYS},
    }


def selected_material(patient, selection, *, lock=False):
    """The caller holds actor/patient authorization; locks follow parent documents then records."""
    ids, query = _records(patient, selection)
    if lock:
        # Deferred import avoids coupling content.py initialization to this module.
        from apps.exports.content import lock_sources
        lock_sources(patient, document_dependencies(patient, selection))
        query = query.select_for_update(of=('self',))
    records = list(query)
    if len(records) != len(ids):
        raise PermissionDenied
    if any(record.deleted_at is not None or not source_current(record) for record in records):
        raise ExportInputError('部分选定血糖记录已删除或来源已变化，请重新核对。')
    revisions = {record_id: (identity, author_id) for record_id, identity, author_id in GlucoseRevision.objects.filter(
        record_id__in=ids, sequence=F('record__revision_number')).values_list('record_id', 'pk', 'author_id')}
    rows, sources, dependencies = [], [], []
    for record in records:
        revision_id, revision_author = revisions.get(record.pk, (None, None))
        row = {
            'id': str(record.pk), 'source_kind': record.source_kind, 'source_kind_label': record.get_source_kind_display(),
            'created_by': str(record.created_by_id) if record.created_by_id else None,
            'updated_by': str(record.updated_by_id) if record.updated_by_id else None,
            'created_at': record.created_at.isoformat(), 'updated_at': record.updated_at.isoformat(),
            'revision_number': record.revision_number, 'revision_id': str(revision_id) if revision_id else None,
            'revision_author': str(revision_author) if revision_author else None,
            'data': _public_data(record.current_data), 'original_data': _public_data(record.original_data),
        }
        source = _source(record)
        rows.append(row)
        sources.append(source)
        # Bind full private source content by digest, while exporting only the
        # selected values and field locations. An unselected report body stays out.
        dependencies.append({'record': row, 'source': source, 'data': record.current_data, 'original': record.original_data})
    return {'records': rows, 'sources': sources, 'fingerprint': digest(dependencies),
            'document_ids': sorted({str(record.source_document_id) for record in records if record.source_document_id})}


def assert_material_current(patient, snapshot):
    try:
        selection = snapshot.get('selection', {})
        ids = identifiers(selection.get('glucose_record_ids', []))
        if not ids and snapshot.get('glucose_fingerprint') is None:
            if snapshot.get('glucose_records') or snapshot.get('glucose_record_sources') or snapshot.get('glucose_document_ids'):
                raise SnapshotChanged
            return
        material = selected_material(patient, selection, lock=True)
        rows, sources = snapshot['glucose_records'], snapshot['glucose_record_sources']
        if (len(rows) != len(ids) or {row['id'] for row in rows} != set(ids)
                or len(sources) != len(ids) or {source['record_id'] for source in sources} != set(ids)
                or snapshot['glucose_document_ids'] != material['document_ids']
                or snapshot['glucose_fingerprint'] != material['fingerprint']):
            raise SnapshotChanged
    except (ExportInputError, PermissionDenied, SnapshotChanged, TypeError, ValueError, KeyError):
        raise SnapshotChanged('选定血糖记录、实际作者或来源已变化，请重新选择并生成。') from None
