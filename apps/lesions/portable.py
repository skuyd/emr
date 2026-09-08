"""Explicit selected graph projection; full dependencies remain private."""
from copy import deepcopy
from dataclasses import asdict
import uuid

from django.core.exceptions import ValidationError

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.selection import identifiers
from apps.facts.readmodels import digest

from .comparison import MeasurementPoint, compare_measurements, measurement_points
from .models import Lesion, LesionObservation
from .readmodels import _one_usable, lesion_state, observation_material


ARRAYS = ('lesions', 'lesion_observations', 'lesion_measurements')
CSV_FIELDS = {
    'lesions': ['id', 'name', 'revision_number', 'semantics'],
    'lesion_observations': ['id', 'lesion_id', 'report_id', 'document_id', 'entity_key', 'revision_number',
                            'status', 'field_ids', 'context_field_ids', 'site_field_ids', 'laterality_scope'],
    'lesion_measurements': ['id', 'observation_id', 'lesion_id', 'report_id', 'field_id', 'kind', 'component_index',
                            'axis', 'role', 'date_value', 'date_reliable', 'method', 'field_conflict', 'raw_value',
                            'raw_unit', 'raw_expression', 'value', 'range_values', 'unit', 'comparator', 'approximate',
                            'conversion', 'report_maximum', 'context_field_ids', 'maximum_field_ids', 'comparison'],
}


def chosen_ids(selection):
    return identifiers(selection.get('lesion_ids', []))


def _records(patient, chosen):
    return LesionObservation.objects.filter(patient=patient, revisions__lesion_id__in=chosen).distinct().order_by('pk')


def document_dependencies(patient, selection):
    chosen = chosen_ids(selection)
    return sorted({str(row.document_id) for row in _records(patient, chosen).filter(document__deleted_at__isnull=True)
                   if row.document_id}) if chosen else []


def _history(record):
    revisions = list(record.revisions.select_related('operation__author').order_by('sequence'))
    values, valid = [], bool(revisions and revisions[-1].sequence == record.revision_number)
    for revision in revisions:
        operation = revision.operation
        available = bool(operation.author_id and operation.author.is_active)
        valid = valid and available
        values.append({'id': str(revision.pk), 'sequence': revision.sequence, 'before': deepcopy(revision.before),
                       'after': deepcopy(revision.after), 'operation': str(operation.pk), 'action': operation.action,
                       'author': str(operation.author_id) if operation.author_id else None, 'author_active': available,
                       'checked_original': operation.checked_original, 'note': operation.note,
                       'reverses': str(operation.reverses_id) if operation.reverses_id else None})
    return {'valid': bool(valid), 'revisions': values}


def selected_material(patient, selection):
    """Trusted patient-locked caller only; this does not expand public scope."""
    chosen = chosen_ids(selection)
    if not chosen:
        return None
    lesions = list(Lesion.objects.filter(patient=patient, pk__in=chosen).select_related('created_by').order_by('pk'))
    if {str(row.pk) for row in lesions} != set(chosen):
        raise ExportInputError('部分病灶标识不属于当前患者或已不可用。')
    records = list(_records(patient, chosen))
    observations = {row['id']: row for row in observation_material(patient, include_unavailable=True)}
    states, history, related, valid = [], {}, [], True
    for row in lesions:
        state = lesion_state(row)
        author_valid = bool(row.created_by_id and row.created_by.is_active)
        head = _history(row)
        valid = valid and state['active'] and author_valid and head['valid']
        states.append(state)
        history['lesion:' + str(row.pk)] = {'creator': str(row.created_by_id) if row.created_by_id else None,
                                           'creator_active': author_valid, 'history': head}
    for record in records:
        row = observations.get(str(record.pk))
        head = _history(record)
        valid = valid and head['valid']
        try:
            record.clean()
        except ValidationError:
            valid = False
        history['observation:' + str(record.pk)] = {
            'report_id': str(record.report_id) if record.report_id else None,
            'document_id': str(record.document_id) if record.document_id else None,
            'original_report_id': str(record.original_report_id), 'entity_key': record.entity_key, 'history': head}
        if row:
            related.append(row)
        else:
            valid = False
    document_ids = document_dependencies(patient, selection)
    fingerprint = digest({'states': states, 'history': history, 'observations': related, 'valid': bool(valid)})
    return {'lesions': states, 'observations': related, 'valid': bool(valid), 'fingerprint': fingerprint,
            'document_ids': document_ids,
            'binding_ids': {'lesion': sorted(chosen), 'observation': sorted(str(row.pk) for row in records)}}


def assert_current(patient, snapshot):
    expected = snapshot.get('lesion_fingerprint')
    if not expected and not chosen_ids(snapshot.get('selection', {})):
        return
    try:
        current = selected_material(patient, {'lesion_ids': snapshot.get('lesion_dependency_ids', chosen_ids(snapshot['selection']))})
    except ExportInputError:
        raise SnapshotChanged('病灶关联来源已变化，请重新核对并选择。') from None
    if not current or not current['valid'] or current['fingerprint'] != expected:
        raise SnapshotChanged('病灶名称、关联、来源或历史作者已变化，请重新核对并生成。')


def _scope(fields):
    parents = {field['id'] for field in fields if field['field_key'] == 'lesion.site' and not field['conflict']}
    whole, named, unknown, conflict = [], [], [], False
    codes = set()
    for field in fields:
        scope = field.get('laterality_scope') or {}
        state = scope.get('scope_state')
        if state == 'WHOLE_ENTITY':
            conflict = conflict or field['conflict']
            if scope.get('parent_field_id') in parents and not field['conflict']:
                whole.append(field['id'])
                codes.add(field['content']['value']['code'])
        elif state == 'NAMED_MEMBERS_ONLY' and not field['conflict']:
            named.append(field['id'])
        elif field['field_key'] == 'lesion.laterality':
            unknown.append(field['id'])
    conflict = conflict or len(codes) > 1
    state = ('CONFLICT' if conflict else 'WHOLE_ENTITY' if whole else 'NAMED_MEMBERS_ONLY' if named
             else 'UNKNOWN_SCOPE' if unknown else 'UNAVAILABLE')
    return {'scope_state': state, 'whole_field_ids': sorted(whole) if not conflict else [],
            'named_field_ids': sorted(named), 'unknown_field_ids': sorted(unknown)}


def _points(row):
    context = row['context_fields']
    context_ids = sorted(field['id'] for field in context if field['field_key'] in {'report.exam_date', 'imaging.modality'})
    maximum_ids = sorted(field['id'] for field in row['fields'] if field['field_key'] == 'lesion.maximum_scope')
    result = []
    for point in measurement_points(row):
        value = asdict(point)
        value.update(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f'emr:lesion-measurement:{row["id"]}:{point.field_id}:{point.component_index}')),
                     value=str(point.value) if point.value is not None else None,
                     range_values=[str(number) for number in point.range_values], method=list(point.method),
                     context_field_ids=context_ids, maximum_field_ids=maximum_ids)
        result.append(value)
    return result


def point_value(row):
    from decimal import Decimal
    value = {key: deepcopy(row[key]) for key in MeasurementPoint.__dataclass_fields__}
    value.update(value=Decimal(row['value']) if row['value'] is not None else None,
                 range_values=tuple(Decimal(item) for item in row['range_values']), method=tuple(row['method']))
    return MeasurementPoint(**value)


def _comparison_rows(points, relationships):
    lookup = {row['id']: row for row in points}
    for row in points:
        relationship = relationships.get(row['id']) or {}
        previous = lookup.get(relationship.get('previous_id'))
        complete = relationship.get('source_context_complete') is True
        comparison = {'previous_id': previous['id'] if previous else None, 'source_context_complete': complete,
                      'comparable': False, 'delta': None, 'reasons': ['selected_source_incomplete']}
        if previous:
            arithmetic = compare_measurements(point_value(previous), point_value(row))
            reasons = list(arithmetic['reasons']) + ([] if complete else ['source_context_incomplete'])
            comparison.update(comparable=not reasons, delta=str(arithmetic['delta']) if not reasons else None,
                              reasons=list(dict.fromkeys(reasons)))
        row['comparison'] = comparison


def _full_relationships(material):
    from .trends import build_trends
    result = {}
    def identity(point):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f'emr:lesion-measurement:{point.observation_id}:{point.field_id}:{point.component_index}'))
    for lesion in material['lesions']:
        full = build_trends(row for row in material['observations'] if row['lesion_id'] == lesion['id'])
        for change in full['comparisons']:
            result[identity(change['current'])] = {'previous_id': identity(change['previous']),
                                                  'source_context_complete': change['comparable']}
    return result


def project_material(material, clinical, selection):
    result = {key: [] for key in ARRAYS}
    if material is None:
        return result
    if not material['valid']:
        raise ExportInputError('病灶关联或原作者已不可用，请重新核对。')
    wanted = set(chosen_ids(selection))
    selected = {field['id'] for field in clinical['clinical_fields']}
    for row in material['observations']:
        if row['lesion_id'] not in wanted or not row['usable']:
            continue
        fields = [field for field in row['fields'] if field['id'] in selected and field['usable']]
        context = [field for field in row['context_fields'] if field['id'] in selected and field['usable']]
        sites = [field for field in fields if field['field_key'] == 'lesion.site' and not field['conflict']]
        if not sites:
            continue
        result['lesion_observations'].append({
            **{key: deepcopy(row[key]) for key in ('id', 'lesion_id', 'report_id', 'document_id', 'entity_key', 'revision_number')},
            'status': 'CONFIRMED', 'field_ids': sorted(field['id'] for field in fields),
            'context_field_ids': sorted(field['id'] for field in context), 'site_field_ids': sorted(field['id'] for field in sites),
            'laterality_scope': _scope([field for field in clinical['clinical_fields'] if field['id'] in {item['id'] for item in fields}]),
        })
        selected_row = {**row, 'fields': fields, 'context_fields': context,
                        'date': _one_usable(context, 'report.exam_date'), 'method': _one_usable(context, 'imaging.modality')}
        result['lesion_measurements'].extend(_points(selected_row))
    used = {row['lesion_id'] for row in result['lesion_observations']}
    if used != wanted:
        raise ExportInputError('请同时选择每个病灶当前已核对观察的部位字段；病灶标识不会自动纳入原件或其他字段。')
    result['lesions'] = [{key: deepcopy(row[key]) for key in ('id', 'name', 'revision_number')} |
                         {'semantics': 'USER_CONFIRMED_GROUPING_NOT_MEDICAL_CONCLUSION'}
                         for row in material['lesions'] if row['id'] in used]
    _comparison_rows(result['lesion_measurements'], _full_relationships(material))
    for key in ARRAYS:
        result[key].sort(key=lambda row: row['id'])
    return result


def reproject(snapshot, clinical_fields, selection):
    """Pure shared/portable projection, rebuilding context from allowed UUIDs."""
    selected = {field['id']: field for field in clinical_fields}
    wanted = set(chosen_ids(selection))
    result = {key: [] for key in ARRAYS}
    for original in snapshot.get('lesion_observations', []):
        if original['lesion_id'] not in wanted:
            continue
        fields = [{**selected[key], 'usable': True} for key in original['field_ids'] if key in selected]
        context = [{**selected[key], 'usable': True} for key in original['context_field_ids'] if key in selected]
        sites = [field['id'] for field in fields if field['field_key'] == 'lesion.site' and not field['conflict']]
        if not sites:
            continue
        row = {**deepcopy(original), 'field_ids': sorted(field['id'] for field in fields),
               'context_field_ids': sorted(field['id'] for field in context), 'site_field_ids': sorted(sites),
               'laterality_scope': _scope(fields)}
        result['lesion_observations'].append(row)
        result['lesion_measurements'].extend(_points({**row, 'usable': True, 'fields': fields, 'context_fields': context,
            'date': _one_usable(context, 'report.exam_date'), 'method': _one_usable(context, 'imaging.modality')}))
    used = {row['lesion_id'] for row in result['lesion_observations']}
    if used != wanted:
        raise ExportInputError('选定病灶缺少当前观察的已选部位字段。')
    result['lesions'] = [deepcopy(row) for row in snapshot.get('lesions', []) if row['id'] in used]
    if {row['id'] for row in result['lesions']} != used:
        raise ExportInputError('选定病灶标识不完整。')
    relationships = {row['id']: row['comparison'] for row in snapshot.get('lesion_measurements', [])}
    _comparison_rows(result['lesion_measurements'], relationships)
    for key in ARRAYS:
        result[key].sort(key=lambda row: row['id'])
    return result


def _omission_validation_view(value, *, for_bounds=False):
    """Separate explicit display omission metadata from the strict typed shape.

    This detached view never changes the delivered value or proves its source.
    Only the cloud projection's true flag with a visible omission is accepted;
    all other keys and the full derived graph still face the existing checks.
    For text bounds only, a flagged marker consumes the shortest redactor match
    (eight characters, http://a). Which markers came from URLs is unknowable;
    this is a bounded display allowance, never a reconstruction of original text.
    """
    from apps.cloud_imaging.projection import OMITTED

    def visit(item, flagged=False):
        if isinstance(item, str):
            bounded = item.replace(OMITTED, 'x' * 8) if for_bounds and flagged else item
            return bounded, OMITTED in item
        if isinstance(item, list):
            children = [visit(child, flagged) for child in item]
            return [child for child, _ in children], any(omitted for _, omitted in children)
        if isinstance(item, dict):
            # A parent flag may describe another child. Each nested object must
            # carry its own annotation before any of its strings gets allowance.
            own_flag = item.get('external_access_omitted') is True
            children = {key: visit(child, own_flag) for key, child in item.items() if key != 'external_access_omitted'}
            omitted = any(found for _, found in children.values())
            if 'external_access_omitted' in item and (item['external_access_omitted'] is not True or not omitted):
                raise ValueError('invalid display omission metadata')
            return {key: child for key, (child, _) in children.items()}, omitted
        return item, False

    return visit(value)[0]


def validate_portable(data):
    """Check new-table closure and exact derived values without database reads."""
    from django.core.exceptions import ValidationError
    from apps.facts.clinical_schema import FIELDS, display_value, validate_value
    from .readmodels import CONTEXT_KEYS, observation_id

    def unique(rows):
        values = {row['id']: row for row in rows}
        if len(values) != len(rows):
            raise ValueError('duplicate identity')
        return values
    try:
        bounded_fields = unique(_omission_validation_view(data['clinical_fields'], for_bounds=True))
        bounded_lesions = unique(_omission_validation_view(data['lesions'], for_bounds=True))
        data = {**data, **{key: _omission_validation_view(data[key]) for key in
                          ('clinical_fields', 'clinical_reports', 'documents', *ARRAYS)}}
        fields, reports, documents = (unique(data[key]) for key in ('clinical_fields', 'clinical_reports', 'documents'))
        for field in fields.values():
            if field['field_key'] not in {'lesion.laterality', 'lesion.scoped_laterality'}:
                continue
            scope = field.get('laterality_scope')
            if not isinstance(scope, dict) or set(scope) != {'scope_state', 'parent_selected', 'parent_field_id'}:
                raise ValueError('missing side scope')
            allowed = {'NAMED_MEMBERS_ONLY'} if field['field_key'] == 'lesion.scoped_laterality' else {'WHOLE_ENTITY', 'UNKNOWN_SCOPE'}
            if scope['scope_state'] not in allowed or type(scope['parent_selected']) is not bool:
                raise ValueError('contradictory scope')
            content, spec = field['content'], FIELDS[field['field_key']]
            validate_value(field['field_key'], bounded_fields[field['id']]['content']['value'])
            # Fine selection intentionally omits raw_value and transformations.
            # Validate the remaining typed identity and display without inventing
            # the missing original context to satisfy the full candidate schema.
            if (content['field_key'] != field['field_key'] or content['schema_version'] != spec.version
                    or content['value_type'] != spec.value_type or content['result_type'] != 'SOURCE_REPORTED'
                    or content['category'] != 'IMAGING'
                    or content['text'] != f"{spec.label}：{display_value(field['field_key'], content['value'])}"):
                raise ValueError('scope content identity or display disagrees')
            if scope['parent_selected']:
                parent = fields[scope['parent_field_id']]
                if parent['field_key'] != 'lesion.site' or (parent['report_id'], parent['entity_key']) != (field['report_id'], field['entity_key']):
                    raise ValueError('wrong parent')
            elif scope['parent_field_id'] is not None:
                raise ValueError('unselected parent')
        lesions = unique(data['lesions'])
        observations = unique(data['lesion_observations'])
        unique(data['lesion_measurements'])
        for row in data['lesion_measurements']:
            comparison = row['comparison']
            if (set(row) != set(CSV_FIELDS['lesion_measurements']) or set(comparison) !=
                    {'previous_id', 'source_context_complete', 'comparable', 'delta', 'reasons'}
                    or type(comparison['source_context_complete']) is not bool or type(comparison['comparable']) is not bool):
                raise ValueError('invalid comparison scope')
        for lesion in lesions.values():
            if (set(lesion) != set(CSV_FIELDS['lesions']) or not isinstance(lesion['name'], str)
                    or not 1 <= len(bounded_lesions[lesion['id']]['name']) <= 120
                    or type(lesion['revision_number']) is not int or lesion['revision_number'] < 1
                    or lesion['semantics'] != 'USER_CONFIRMED_GROUPING_NOT_MEDICAL_CONCLUSION'):
                raise ValueError('invalid identity state')
            uuid.UUID(lesion['id'])
        for row in observations.values():
            if (set(row) != set(CSV_FIELDS['lesion_observations']) or row['lesion_id'] not in lesions
                    or row['status'] != 'CONFIRMED' or type(row['revision_number']) is not int or row['revision_number'] < 1
                    or row['id'] != observation_id(row['report_id'], row['entity_key'])
                    or reports[row['report_id']]['document_id'] != row['document_id'] or row['document_id'] not in documents):
                raise ValueError('invalid observation relationship')
            for key in ('field_ids', 'context_field_ids', 'site_field_ids'):
                if not isinstance(row[key], list) or len(set(row[key])) != len(row[key]):
                    raise ValueError('duplicate field references')
            for identity in row['field_ids'] + row['context_field_ids']:
                field = fields[identity]
                if field['report_id'] != row['report_id'] or field['content']['field_key'] != field['field_key']:
                    raise ValueError('cross-report field')
                validate_value(field['field_key'], bounded_fields[field['id']]['content']['value'])
                if identity in row['field_ids']:
                    if field['entity_key'] != row['entity_key'] or not field['field_key'].startswith('lesion.'):
                        raise ValueError('cross-entity field')
                elif field['field_key'] not in CONTEXT_KEYS:
                    raise ValueError('invalid context')
        rebuilt = reproject(data, list(fields.values()), {'lesion_ids': sorted(lesions)})
        if any(sorted(data[key], key=lambda row: row['id']) != rebuilt[key] for key in ARRAYS):
            raise ValueError('derived value or scope disagrees with selected field')
    except (ValidationError, KeyError, TypeError, ValueError, AttributeError):
        raise ExportInputError('病灶关联表、侧别作用范围或测量来源不完整或矛盾。') from None


def shared_material(snapshot, fields, scope):
    result = reproject(snapshot, fields, scope)
    result.update(lesion_fingerprint=snapshot.get('lesion_fingerprint'),
                  lesion_binding_ids=deepcopy(snapshot.get('lesion_binding_ids', {})),
                  lesion_document_ids=deepcopy(snapshot.get('lesion_document_ids', [])),
                  lesion_dependency_ids=deepcopy(snapshot.get('lesion_dependency_ids', [])))
    return result


def bind_output(output, snapshot, *, sharing=False):
    from .models import LesionExportSource, LesionShareSource
    model, key = (LesionShareSource, 'share') if sharing else (LesionExportSource, 'job')
    for kind, ids in snapshot.get('lesion_binding_ids', {}).items():
        if kind not in {'lesion', 'observation'}:
            raise ExportInputError('病灶来源绑定类型无效。')
        for identity in ids:
            row = model(**{key: output, 'kind': kind.upper(), 'original_id': identity, kind + '_id': identity})
            row.full_clean()
            row.save()


def bindings_current(output, snapshot):
    expected = snapshot.get('lesion_binding_ids', {})
    actual = {key: [] for key in expected}
    for row in output.lesion_sources.all():
        key = row.kind.lower()
        if key not in {'lesion', 'observation'}:
            return False
        identity = getattr(row, key + '_id')
        other = row.observation_id if key == 'lesion' else row.lesion_id
        if identity is None or identity != row.original_id or other is not None:
            return False
        actual.setdefault(key, []).append(str(identity))
    return {key: sorted(ids) for key, ids in expected.items()} == {key: sorted(ids) for key, ids in actual.items()}
