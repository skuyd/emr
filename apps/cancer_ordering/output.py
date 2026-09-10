"""Current selected statements; no automatic diagnosis or whole-source grant."""

from copy import deepcopy
from uuid import UUID

from apps.exports.errors import ExportInputError

from .exporting import ARRAYS, OMITTED_SOURCE, restrict_source
from .forms import ASSERTION_LABELS, MODE_LABELS, STATUS_LABELS, SUBJECT_LABELS
from .labels import REASONS
from .matching import ALIASES
from .profiles import PROFILE_LABELS


CSV_FIELDS = {
    'cancer_candidates': ['id', 'label', 'assertion', 'subject', 'status', 'value_origin', 'source'],
    'indicator_ordering': ['mode', 'profile', 'reason', 'candidate_id'],
}
ORIGIN_LABELS = {'REPORT': '报告表述', 'SOURCE_TRANSCRIPTION': '原件补录', 'MANUAL_CORRECTION': '人工更正'}


def _require(condition):
    if not condition:
        raise ExportInputError('报告表述或显示偏好的关联内容无效。')


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def validate_portable(data):
    candidates, choices = (data[key] for key in ARRAYS)
    if not candidates and not choices:
        return
    ids = set()
    documents = {row['id'] for row in data['documents'] if isinstance(row, dict) and isinstance(row.get('id'), str)}
    facts = {row['id']: row for row in (*data['facts'], *data['clinical_fields'])
             if isinstance(row, dict) and isinstance(row.get('id'), str)}
    for row in candidates:
        _require(isinstance(row, dict) and set(row) == set(CSV_FIELDS['cancer_candidates']))
        identity = row['id']
        _require(_uuid(identity) and identity not in ids)
        ids.add(identity)
        _require(isinstance(row['label'], str) and bool(row['label'].strip()) and len(row['label']) <= 160)
        _require(isinstance(row['assertion'], str) and row['assertion'] in ASSERTION_LABELS)
        _require(isinstance(row['subject'], str) and row['subject'] in SUBJECT_LABELS)
        _require(isinstance(row['status'], str) and row['status'] in {'PENDING', 'CONFIRMED'})
        _require(isinstance(row['value_origin'], str) and row['value_origin'] in ORIGIN_LABELS)
        source = row['source']
        _require(isinstance(source, dict))
        if source == OMITTED_SOURCE:
            continue
        _require(set(source) == {'state', 'document_id', 'fact_id', 'page', 'location'}
                 and source.get('state') == 'SELECTED_REFERENCE')
        _require(_uuid(source['fact_id']) and _uuid(source['document_id']))
        _require(source['fact_id'] in facts and source['document_id'] in documents)
        actual = facts[source['fact_id']].get('source')
        _require(isinstance(actual, dict) and type(source['page']) is int and source['page'] >= 1)
        _require(all(source[key] == actual.get(key) for key in ('document_id', 'page', 'location')))
    _require(len(choices) <= 1)
    reasons_by_mode = {
        'AUTO': {'collection_incomplete', 'no_reported_diagnosis', 'reported_diagnoses_differ',
                 'unsupported_reported_diagnosis', 'original_review_required', 'reported_diagnosis'},
        'GENERAL': {'explicit_general'}, 'MANUAL_PROFILE': {'manual_display_preference'},
        'CANDIDATE': {'selected_reported_diagnosis', 'selection_source_changed'},
    }
    for row in choices:
        _require(isinstance(row, dict) and set(row) == set(CSV_FIELDS['indicator_ordering']))
        _require(isinstance(row['mode'], str) and row['mode'] in MODE_LABELS)
        _require(isinstance(row['profile'], str) and row['profile'] in PROFILE_LABELS)
        _require(isinstance(row['reason'], str) and row['reason'] in REASONS)
        _require(row['reason'] in reasons_by_mode[row['mode']] | {'selection_author_or_configuration_changed'})
        if row['candidate_id'] is not None:
            _require(_uuid(row['candidate_id']) and row['mode'] == 'CANDIDATE' and row['candidate_id'] in ids)
        successful = {'reported_diagnosis': 'AUTO', 'selected_reported_diagnosis': 'CANDIDATE',
                      'manual_display_preference': 'MANUAL_PROFILE'}
        if row['reason'] in successful:
            _require(row['mode'] == successful[row['reason']] and row['profile'] in {'LUNG', 'PANCREAS'})
            if row['candidate_id'] is not None:
                candidate = next(item for item in candidates if item['id'] == row['candidate_id'])
                _require(ALIASES.get(candidate['label']) == row['profile']
                         and candidate['assertion'] == 'AFFIRMED' and candidate['subject'] == 'CURRENT_PRIMARY')
        else:
            _require(row['profile'] == 'GENERAL')
            if row['reason'] == 'explicit_general':
                _require(row['mode'] == 'GENERAL')


def card_entries(snapshot):
    entries = []
    for row in snapshot.get('cancer_candidates', []):
        text = ' · '.join((ORIGIN_LABELS[row['value_origin']], row['label'], ASSERTION_LABELS[row['assertion']],
                           SUBJECT_LABELS[row['subject']], STATUS_LABELS[row['status']]))
        source = row['source']
        text += (f"；选定来源第 {source['page']} 页" if source['state'] == 'SELECTED_REFERENCE'
                 else '；来源内容未纳入本次选择')
        entries.append({'text': text})
    for row in snapshot.get('indicator_ordering', []):
        entries.append({'text': f"指标显示偏好：{PROFILE_LABELS[row['profile']]}。"
                        + REASONS[row['reason']].replace('使用你', '使用') + '仅用于排列指标，不代表诊断确认。'})
    return entries


def share_material(snapshot, scope, projected):
    wanted = set(scope.get('cancer_candidate_ids', [])) if 'cancer_ordering' in scope['sections'] else set()
    rows = [deepcopy(row) for row in snapshot.get('cancer_candidates', []) if row['id'] in wanted]
    _require(wanted == {row['id'] for row in rows})
    documents = {row['id'] for row in projected['documents']}
    facts = {row['id'] for row in (*projected['facts'], *projected['clinical_fields'])}
    for row in rows:
        row['source'] = restrict_source(row['source'], documents, facts)
    choices = deepcopy(snapshot.get('indicator_ordering', [])) if (
        'cancer_ordering' in scope['sections'] and scope.get('include_indicator_ordering')) else []
    for row in choices:
        if row['candidate_id'] not in wanted:
            row['candidate_id'] = None
    private = {key: deepcopy(snapshot[key]) for key in ('cancer_ordering_version', 'cancer_ordering_fingerprint') if key in snapshot}
    return {'cancer_candidates': rows, 'indicator_ordering': choices, **private}
