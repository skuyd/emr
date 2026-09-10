"""Narrow public tables and durable output bindings for selected cloud sources."""
from copy import deepcopy
import math
import re
from uuid import UUID

from django.core.exceptions import ValidationError

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.selection import identifiers

from .exporting import ARRAYS
from .url_policy import validate_url

SOURCE_FIELDS = ('id', 'revision_number', 'document_id', 'page_id', 'page', 'report_id',
                 'evidence_id', 'status', 'kind', 'site_label', 'title', 'current_url')
EVIDENCE_FIELDS = ('id', 'source_id', 'document_id', 'page_id', 'page', 'kind', 'polygon',
                   'start_offset', 'end_offset', 'ocr_block_id', 'parsing_version_id',
                   'decoder_version', 'payload_type', 'input_sha256', 'render_profile', 'transform')
CSV_FIELDS = dict(zip(ARRAYS, (SOURCE_FIELDS, EVIDENCE_FIELDS)))
SHARED_FIELDS = ('id', 'revision_number', 'document_id', 'page_id', 'page', 'report_id',
                 'evidence_id', 'status', 'kind', 'site_label')


def bind_output(output, snapshot, *, sharing=False):
    from .models import CloudExportSource, CloudShareSource
    model, field = (CloudShareSource, 'share') if sharing else (CloudExportSource, 'job')
    model.objects.bulk_create([model(**{field: output, **row,
        'source_id': row['source_identity'], 'evidence_id': row['evidence_identity'],
        'document_id': row['document_identity']}) for row in snapshot.get('cloud_binding_ids', [])])


def bindings_current(output, snapshot):
    expected = snapshot.get('cloud_binding_ids', [])
    selected = identifiers(snapshot.get('selection', {}).get('cloud_source_ids', []))
    actual = list(output.cloud_sources.order_by('source_identity').values(
        'source_id', 'evidence_id', 'document_id', 'source_identity', 'evidence_identity',
        'document_identity', 'revision_number', 'source_token'))
    if len(actual) != len(expected) or len(actual) != len(selected):
        return False
    by_id = {row['source_identity']: row for row in expected}
    if set(by_id) != set(selected):
        return False
    for row in actual:
        for key in ('source', 'evidence', 'document'):
            if row[key + '_id'] is None or row[key + '_id'] != row[key + '_identity']:
                return False
        frozen = {k: str(v) if isinstance(v, UUID) else v for k, v in row.items() if not k.endswith('_id')}
        if by_id.get(str(row['source_identity'])) != frozen:
            return False
    return True


def share_material(snapshot):
    return {'cloud_imaging_sources': [{key: deepcopy(row[key]) for key in SHARED_FIELDS}
                                     for row in snapshot.get('cloud_imaging_sources', [])],
            'cloud_imaging_evidence': [],
            **{key: deepcopy(snapshot.get(key)) for key in ('cloud_binding_ids', 'cloud_document_ids', 'cloud_fingerprint')}}


def _uuid(value, *, nullable=False):
    if nullable and value is None:
        return True
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def valid_source_row(row):
    if not isinstance(row, dict) or set(row) - set(SOURCE_FIELDS) - {'external_access_omitted'} or set(SOURCE_FIELDS) - set(row):
        return False
    if 'external_access_omitted' in row and row['external_access_omitted'] is not True:
        return False
    if not all(_uuid(row[key]) for key in ('id','document_id','page_id','evidence_id')) or not _uuid(row['report_id'], nullable=True):
        return False
    if (type(row['revision_number']) is not int or row['revision_number'] < 1
            or type(row['page']) is not int or row['page'] < 1 or row['status'] != 'CONFIRMED'
            or row['kind'] not in {'OCR','QR','MANUAL'} or not isinstance(row['title'], str) or len(row['title']) > 160):
        return False
    try:
        target = validate_url(row['current_url'])
        return target.site_label == row['site_label']
    except (ValidationError, TypeError, AttributeError):
        return False


def allowed_selected_url(value, path, parents):
    if len(path) != 3 or path[0] != ARRAYS[0] or type(path[1]) is not int or path[2] != 'current_url':
        return False
    root, row = parents[0], parents[-1]
    scope = root.get('selection', root.get('scope', {}))
    return (isinstance(scope, dict) and isinstance(scope.get('cloud_source_ids', []), list)
            and isinstance(row, dict) and row.get('id') in scope.get('cloud_source_ids', [])
            and valid_source_row(row) and value == row['current_url'])


def validate_portable(data):
    """Validate table shapes and joins; reading offline never grants live access."""
    try:
        rows, evidence = (data[key] for key in ARRAYS)
        if not isinstance(rows, list) or not isinstance(evidence, list):
            raise ValueError
        scope = data.get('scope', data.get('selection', {}))
        ids = identifiers(scope.get('cloud_source_ids', []))
        if len(rows) != len(ids) or {row['id'] for row in rows} != set(ids) or len(evidence) != len(rows):
            raise ValueError
        if any(not valid_source_row(row) for row in rows):
            raise ValueError
        by_id = {row['id']: row for row in rows}
        seen = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != set(EVIDENCE_FIELDS):
                raise ValueError
            source = by_id[item['source_id']]
            if item['source_id'] in seen:
                raise ValueError
            seen.add(item['source_id'])
            if item['id'] != source['evidence_id'] or any(item[key] != source[key] for key in ('document_id','page_id','page','kind')):
                raise ValueError
            if item['payload_type'] != 'URL' or not _uuid(item['ocr_block_id'], nullable=True) or not _uuid(item['parsing_version_id'], nullable=True):
                raise ValueError
            if (not isinstance(item['decoder_version'],str) or not isinstance(item['input_sha256'],str)
                    or not isinstance(item['render_profile'], str) or not isinstance(item['transform'], list)):
                raise ValueError
            polygon=item['polygon']
            if polygon is not None and (not isinstance(polygon,list) or len(polygon)<3 or any(
                    not isinstance(point,list) or len(point)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point) for point in polygon)):
                raise ValueError
            if item['kind']=='OCR':
                if (not item['ocr_block_id'] or not item['parsing_version_id'] or type(item['start_offset']) is not int
                        or type(item['end_offset']) is not int or not 0 <= item['start_offset'] < item['end_offset']
                        or any(item[key] for key in ('decoder_version','input_sha256','render_profile','transform'))):
                    raise ValueError
            elif any(item[key] is not None for key in ('ocr_block_id','start_offset','end_offset')):
                raise ValueError
            if item['kind']=='MANUAL' and any(item[key] for key in ('polygon','decoder_version','input_sha256','render_profile','transform')):
                raise ValueError
            if item['kind']=='QR':
                from .models import _valid_transform
                if not re.fullmatch('[0-9a-f]{64}', item['input_sha256']) or not item['decoder_version'] or not item['render_profile']:
                    raise ValueError
                if polygon is not None and not _valid_transform(item['transform']):
                    raise ValueError
        from .projection import assert_safe_snapshot
        assert_safe_snapshot({**{key:data[key] for key in ARRAYS}, 'scope':{'cloud_source_ids':ids}})
    except (ValueError, KeyError, TypeError, AttributeError, ExportInputError, SnapshotChanged):
        raise ExportInputError('云影像来源关联表、明确选择或证据结构无效。') from None


def card_entries(snapshot):
    return [{'text': f"站点：{row['site_label']}；{row['current_url']}；原件 {row['document_id']} 第 {row['page']} 页；"
                     f"来源 {row['id']}，修订 {row['revision_number']}（{row['kind']}）。"}
            for row in snapshot.get('cloud_imaging_sources', [])]
