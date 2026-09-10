"""Current explicitly selected cloud sources; provenance does not grant originals."""
from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.views.decorators.debug import sensitive_variables

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.selection import identifiers
from apps.facts.readmodels import digest

from .models import CloudImagingSource
from .readmodels import source_material, source_queryset

OUTPUT_RULE = 'cloud-selected-output-v1'
ARRAYS = ('cloud_imaging_sources', 'cloud_imaging_evidence')


def _query(patient, selection):
    ids = identifiers(selection.get('cloud_source_ids', []))
    query = source_queryset().filter(patient=patient, pk__in=ids).order_by('pk')
    if query.count() != len(ids):
        raise ExportInputError('部分选定云影像来源不可用，请重新选择。')
    return ids, query


def document_dependencies(patient, selection):
    _, query = _query(patient, selection)
    return sorted(str(pk) for pk in query.values_list('document_id', flat=True).distinct())


def _restrict_reports(patient, selection, rows):
    if not rows:
        return
    allowed = None
    if selection.get('report_ids') is not None:
        allowed = set(identifiers(selection['report_ids']))
    if selection.get('clinical_field_ids') is not None:
        from apps.facts.models import Fact
        fields = identifiers(selection['clinical_field_ids'])
        query = Fact.objects.filter(pk__in=fields, document__patient=patient)
        if query.count() != len(fields):
            raise ExportInputError('选定字段范围无效。')
        reports = {str(pk) for pk in query.values_list('clinical_report_id', flat=True) if pk}
        allowed = reports if allowed is None else allowed & reports
    if allowed is not None and any(row['report_id'] not in allowed for row in rows):
        raise ExportInputError('选定云影像来源不属于所选报告或字段范围，请分别选择。')


@sensitive_variables()
def selected_material(patient, selection, *, lock=False, expected=True):
    ids, query = _query(patient, selection)
    if lock:
        query = query.select_for_update(of=('self',))
    tokens = selection.get('cloud_source_tokens')
    if tokens is not None and (not isinstance(tokens, dict) or set(tokens) != set(ids)):
        raise ExportInputError('云影像选项已变化，请重新选择。')
    rows, evidence_rows, dependencies, bindings = [], [], [], []
    for source in query:
        try:
            source.clean()
            material = source_material(source)
        except (ValidationError, AttributeError, CloudImagingSource.DoesNotExist):
            raise ExportInputError('选定云影像来源证明已变化，请重新核对。') from None
        if not material['usable']:
            raise ExportInputError('选定云影像来源尚未确认或已失效，请重新核对。')
        if expected and tokens is not None and tokens.get(material['id']) != material['source_token']:
            raise ExportInputError('云影像选项已变化，请重新选择。')
        evidence = source.evidence
        rows.append({'id': material['id'], 'revision_number': material['revision_number'],
                     'document_id': material['document_id'], 'page_id': str(evidence.document_page_id),
                     'page': evidence.document_page.page_number, 'report_id': material['report_id'],
                     'evidence_id': str(evidence.pk), 'status': 'CONFIRMED', 'kind': evidence.kind,
                     'site_label': source.site_label, 'title': source.title, 'current_url': source.current_url})
        evidence_rows.append({'id': str(evidence.pk), 'source_id': str(source.pk),
                     'document_id': str(source.document_id), 'page_id': str(evidence.document_page_id),
                     'page': evidence.document_page.page_number, 'kind': evidence.kind,
                     'polygon': deepcopy(evidence.polygon), 'start_offset': evidence.start_offset,
                     'end_offset': evidence.end_offset, 'ocr_block_id': str(evidence.ocr_block_id) if evidence.ocr_block_id else None,
                     'parsing_version_id': str(evidence.parsing_version_id) if evidence.parsing_version_id else None,
                     'decoder_version': evidence.decoder_version, 'payload_type': evidence.payload_type,
                     'input_sha256': evidence.input_sha256, 'render_profile': deepcopy(evidence.render_profile),
                     'transform': deepcopy(evidence.transform)})
        bindings.append({'source_identity': str(source.pk), 'evidence_identity': str(evidence.pk),
                         'document_identity': str(source.document_id), 'revision_number': source.revision_number,
                         'source_token': material['source_token']})
        dependencies.append({'id': material['id'], 'token': material['source_token']})
    _restrict_reports(patient, selection, rows)
    return {'cloud_imaging_sources': rows, 'cloud_imaging_evidence': evidence_rows,
            'cloud_binding_ids': bindings,
            'cloud_document_ids': sorted({row['document_id'] for row in rows}),
            'cloud_fingerprint': digest({'rule': OUTPUT_RULE, 'sources': dependencies})}


def assert_material_current(patient, snapshot):
    try:
        selection = snapshot.get('selection', {})
        ids = identifiers(selection.get('cloud_source_ids', []))
        if not ids and snapshot.get('cloud_fingerprint') is None:
            if any(snapshot.get(key) for key in (*ARRAYS, 'cloud_binding_ids', 'cloud_document_ids')):
                raise SnapshotChanged
            return
        material = selected_material(patient, selection, lock=True, expected=False)
        for key in ('cloud_binding_ids', 'cloud_document_ids', 'cloud_fingerprint'):
            if snapshot.get(key) != material[key]:
                raise SnapshotChanged
        # Sharing removes URLs and private proof details, but binds the exact
        # stored projection by its own digest and the same private source token.
        if {row['id'] for row in snapshot.get('cloud_imaging_sources', [])} != set(ids):
            raise SnapshotChanged
        from .projection import project_default_snapshot
        from .output import share_material
        projected = project_default_snapshot({**material, 'selection': {'cloud_source_ids': ids}})
        shared = share_material(projected)
        actual = {key: snapshot.get(key, []) for key in ARRAYS}
        if actual not in ({key: projected[key] for key in ARRAYS}, {key: shared[key] for key in ARRAYS}):
            raise SnapshotChanged
    except (ExportInputError, PermissionDenied, SnapshotChanged, KeyError, TypeError, ValueError):
        raise SnapshotChanged('选定云影像来源、实际作者或来源证明已变化，请重新生成。') from None
