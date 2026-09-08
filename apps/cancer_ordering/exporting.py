"""Explicit current statements, display choice and private ordering dependencies."""

from copy import deepcopy
import hmac
import re

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied

from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.selection import identifiers

from .models import CancerCandidate
from .profiles import prioritize
from .readmodels import resolve_ordering


ARRAYS = ('cancer_candidates', 'indicator_ordering')
OUTPUT_VERSION = 'cancer-selected-output-1'
OMITTED_SOURCE = {'state': 'OMITTED', 'reason': 'SOURCE_CONTENT_NOT_SELECTED'}


def normalized_selection(selection):
    chosen = identifiers(selection.get('cancer_candidate_ids', []))
    include = selection.get('include_indicator_ordering', False)
    if type(include) is not bool:
        raise ExportInputError('请选择是否携带指标显示偏好。')
    sections = selection.get('sections', [])
    if (chosen or include) and (not isinstance(sections, list) or 'cancer_ordering' not in sections):
        raise ExportInputError('请选择报告表述与显示偏好的展示范围。')
    return {'cancer_candidate_ids': chosen, 'include_indicator_ordering': include}


def has_selection(selection):
    return bool(selection.get('cancer_candidate_ids') or selection.get('include_indicator_ordering'))


def selectable_candidate(row):
    return (row['source_valid'] and not row['source_changed']
            and row['status'] not in {'EXCLUDED', 'DEFERRED'}
            and row['parent_status'] not in {'EXCLUDED', 'DEFERRED'})


def restrict_source(source, document_ids, fact_ids):
    if (source.get('state') == 'SELECTED_REFERENCE' and source.get('document_id') in document_ids
            and source.get('fact_id') in fact_ids):
        return {key: deepcopy(source[key]) for key in ('state', 'document_id', 'fact_id', 'page', 'location')}
    return deepcopy(OMITTED_SOURCE)


def selected_material(patient, selection, *, has_labs=False, documents=(), facts=(), clinical_fields=()):
    selected = normalized_selection(selection)
    wanted = selected['cancer_candidate_ids']
    owned = {str(row.pk): row for row in CancerCandidate.objects.filter(patient=patient, pk__in=wanted)}
    if set(wanted) != set(owned):
        raise PermissionDenied
    if not has_labs and not has_selection(selected):
        return {'profile': 'GENERAL', 'fingerprint': None, **{key: [] for key in ARRAYS}}
    try:
        state = resolve_ordering(patient)
    except ObjectDoesNotExist:
        raise SnapshotChanged('报告来源已变化，请刷新后重新选择。') from None
    if has_selection(selected):
        expected = selection.get('cancer_expected_fingerprint')
        if not valid_fingerprint(expected) or not hmac.compare_digest(expected, state['fingerprint']):
            raise ExportInputError('报告表述或显示偏好已变化，请刷新后重新选择。')
    current = {row['id']: row for row in state['candidates']}
    if any(identity not in current or not selectable_candidate(current[identity]) for identity in wanted):
        raise ExportInputError('选定报告表述已变化、排除或暂缓，请重新核对。')
    document_ids = {row['id'] for row in documents}
    fact_ids = {row['id'] for row in (*facts, *clinical_fields)}
    candidates = []
    for identity in wanted:
        row, model = current[identity], owned[identity]
        source = {'state': 'SELECTED_REFERENCE', 'document_id': row['document_id'],
                  'fact_id': str(model.source_fact_id), 'page': row['source']['page'], 'location': row['source']['location']}
        candidates.append({'id': identity, 'label': row['content']['label'],
            'assertion': row['content']['assertion'], 'subject': row['content']['subject'], 'status': row['status'],
            'value_origin': 'MANUAL_CORRECTION' if row['manual_correction'] else
                            'SOURCE_TRANSCRIPTION' if row['binding_kind'] == 'TRANSCRIBED' else 'REPORT',
            'source': restrict_source(source, document_ids, fact_ids)})
    choice = []
    if selected['include_indicator_ordering']:
        selected_id = state['selection_state'].get('candidate_id') if state['mode'] == 'CANDIDATE' else None
        choice = [{'mode': state['mode'], 'profile': state['profile'], 'reason': state['reason'],
                   'candidate_id': selected_id if selected_id in wanted else None}]
    return {'profile': state['profile'], 'fingerprint': state['fingerprint'],
            'cancer_candidates': candidates, 'indicator_ordering': choice}


def apply_order(labs, card, profile):
    """Reorder the allowed material after existing selections and calculations."""
    labs = list(prioritize(labs, profile, code_of=lambda row: row['standard_code']))
    by_id = {row['id']: row for row in labs}
    card = deepcopy(card)
    card['lab_ids'] = list(prioritize(card['lab_ids'], profile, code_of=lambda identity: by_id[identity]['standard_code']))
    card['trends'] = list(prioritize(card['trends'], profile, code_of=lambda row: row['standard_code']))
    return labs, card


def assert_current(patient, snapshot):
    if 'cancer_ordering_version' not in snapshot:
        return  # Previous snapshots did not apply this ordering contract.
    if snapshot['cancer_ordering_version'] != OUTPUT_VERSION or 'cancer_ordering_fingerprint' not in snapshot:
        raise SnapshotChanged('报告表述输出版本已变化，请重新生成。')
    expected = snapshot['cancer_ordering_fingerprint']
    if expected is None:
        return
    try:
        current = resolve_ordering(patient)['fingerprint']
    except ObjectDoesNotExist:
        raise SnapshotChanged('报告来源已变化，请重新选择并生成。') from None
    if not valid_fingerprint(expected) or not hmac.compare_digest(expected, current):
        raise SnapshotChanged('报告表述、完整来源或显示偏好已变化，请重新选择并生成。')


def valid_fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None
