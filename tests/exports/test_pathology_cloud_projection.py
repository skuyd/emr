"""Real selected pathology outputs remain usable after external access omission."""
from copy import deepcopy
import json

import pytest

from apps.cloud_imaging.projection import OMITTED
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import card_sections
from apps.facts.models import Fact, FactRevision
from apps.facts.readmodels import effective_fact
from apps.patients.sharing import create_share
from tests.exports.test_pathology_exports import _graph, selection
from tests.facts.pathology_factories import add_field, review, score_value


pytestmark = pytest.mark.django_db
URL = 'http://a'


def bounded_text(limit):
    prefix = '标本甲；检测甲；PD-L1；'
    return prefix + '合' * (limit - len(prefix) - len(URL)) + URL


@pytest.mark.parametrize('kind,maximum', [
    ('text', False), ('text', True), ('assertion', False), ('assertion', True),
    ('identity', False), ('identity', True), ('coded', False), ('coded', True),
    ('nodes', False), ('nodes', True), ('score_unit', False), ('score_unit', True),
])
def test_real_selected_url_values_roundtrip_share_and_keep_qualifiers(django_user_model, kind, maximum):
    _, patient, document, report, fields = _graph(django_user_model, 'path-cloud-' + kind + str(maximum))
    targets = {'SPECIMEN': fields['specimen']}
    entity = 'specimen:a'
    if kind == 'text':
        key, value = 'specimen.site', {'text': bounded_text(30000) if maximum else '合成部位 ' + URL}
    elif kind == 'assertion':
        key, value = 'specimen.histology', {'text': bounded_text(30000) if maximum else '不能排除合成结果 ' + URL,
                                          'assertion': 'UNCERTAIN'}
    elif kind == 'identity':
        key, value, targets, entity = 'specimen.identity', {'label': bounded_text(256) if maximum else '合成标本 ' + URL,
                                                          'raw': '合成标本原文'}, {}, 'specimen:extra'
    elif kind == 'coded':
        key, entity = 'assay.method', 'assay:a'
        targets['ASSAY'] = fields['assay']
        value = {'code': 'IHC', 'raw': bounded_text(512) if maximum else '合成方法 ' + URL}
    elif kind == 'nodes':
        key, value = 'specimen.nodes', {'groups': [{'label': bounded_text(256) if maximum else '合成甲组 ' + URL,
            'sampled': None, 'positive': '1', 'raw': '合成计数原文'}],
            'assertion': 'SOURCE_TEXT_ONLY_NOT_DIAGNOSED', 'raw': '合成计数原文'}
    else:
        key, entity = 'ihc.score', 'ihc:a'
        targets.update(ASSAY=fields['assay'], MARKER=fields['marker'])
        value = score_value('IC', '17', bounded_text(30) if maximum else URL)
    fact = add_field(patient, report, key, entity, value, targets,
                     raw=value['text'] if kind in {'text', 'assertion'} and maximum else None)
    review(patient, fact)
    assert effective_fact(Fact.objects.get(pk=fact.pk))['usable']
    original_facts = list(Fact.objects.filter(document=document).values('id', 'automatic_content', 'raw_text', 'revision_number'))
    original_revisions = list(FactRevision.objects.filter(fact__document=document).values())
    chosen = selection(document, fact)
    snapshot = build_snapshot(patient, chosen)
    field = snapshot['clinical_fields'][0]
    text = field['content']['text']
    assert URL not in text and OMITTED in text
    assert '选定标本 1' in text
    if kind == 'assertion':
        assert '原文不确定' in text
    if kind == 'nodes':
        assert '：检出 未提供，阳性 1' in text
    if kind == 'score_unit':
        assert 'IC 17' in text and '按原文记载，未由数值推断阳性' in text and '不可据此判断可比' in text
    data = json.loads(json_bytes(snapshot))
    assert read_structured_data(json.dumps(data)) == data
    public = json.dumps(data, ensure_ascii=False)
    public += ''.join(table.decode('utf-8-sig') for table in csv_tables(snapshot).values())
    public += json.dumps(card_sections(snapshot), ensure_ascii=False)
    assert URL not in public and OMITTED in public
    shared = create_share(patient, patient.account, chosen).share
    assert shared.snapshot['clinical_fields'][0]['content']['value'] == field['content']['value']
    assert '选定标本 1' in shared.snapshot['clinical_fields'][0]['content']['text']
    assert_snapshot_current(patient, snapshot)
    assert_snapshot_current(patient, shared.snapshot)
    assert list(Fact.objects.filter(document=document).values('id', 'automatic_content', 'raw_text', 'revision_number')) == original_facts
    assert list(FactRevision.objects.filter(fact__document=document).values()) == original_revisions


@pytest.mark.parametrize('mutation', ['false_flag', 'no_marker', 'extra_key', 'oversize_unflagged', 'oversize_flagged',
                                    'inherited_flag', 'invalid_assertion'])
def test_reader_does_not_treat_omission_as_permission_to_accept_invalid_values(django_user_model, mutation):
    _, patient, document, report, fields = _graph(django_user_model, 'path-cloud-invalid-' + mutation)
    fact = add_field(patient, report, 'specimen.histology', 'specimen:a',
                     {'text': '合成结果', 'assertion': 'UNCERTAIN'}, {'SPECIMEN': fields['specimen']})
    review(patient, fact)
    data = json.loads(json_bytes(build_snapshot(patient, selection(document, fact))))
    content = data['clinical_fields'][0]['content']
    value = content['value']
    if mutation == 'false_flag':
        value.update(text=OMITTED, external_access_omitted=False)
    elif mutation == 'no_marker':
        value['external_access_omitted'] = True
    elif mutation == 'extra_key':
        value.update(text=OMITTED, external_access_omitted=True, unrecognized='synthetic')
    elif mutation == 'oversize_unflagged':
        value['text'] = bounded_text(30001).replace(URL, OMITTED)
    elif mutation == 'oversize_flagged':
        value.update(text=bounded_text(30001).replace(URL, OMITTED), external_access_omitted=True)
    elif mutation == 'inherited_flag':
        content['external_access_omitted'] = True
        value['text'] = bounded_text(30000).replace(URL, OMITTED)
    else:
        value.update(text=OMITTED, assertion='INFERRED_DIAGNOSIS', external_access_omitted=True)
    original = deepcopy(data)
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))
    assert data == original


def test_changed_private_url_invalidates_snapshot_even_when_visible_text_is_equal(django_user_model):
    _, patient, document, report, fields = _graph(django_user_model, 'path-cloud-private-change')
    fact = add_field(patient, report, 'specimen.site', 'specimen:a', {'text': '合成部位 ' + URL},
                     {'SPECIMEN': fields['specimen']})
    review(patient, fact)
    chosen = selection(document, fact)
    original = build_snapshot(patient, chosen)
    assert_snapshot_current(patient, original)
    review(patient, fact, 'CORRECT', {'value': {'text': '合成部位 http://b'}, 'raw_value': '合成部位 http://b'})
    current = build_snapshot(patient, chosen)
    assert current['clinical_fields'][0]['content']['value'] == original['clinical_fields'][0]['content']['value']
    assert current['dependency_fingerprint'] != original['dependency_fingerprint']
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, original)


@pytest.mark.parametrize('kind', ['specimen', 'assay'])
def test_share_does_not_collapse_distinct_specimen_scopes_when_keys_are_omitted(django_user_model, kind):
    _, patient, document, report, fields = _graph(django_user_model, 'path-cloud-distinct-scopes-' + kind)
    selected = []
    for index, label in enumerate(('标本乙', '标本丙')):
        entity = kind + ':http://' + ('a' if index == 0 else 'b')
        targets = {} if kind == 'specimen' else {'SPECIMEN': fields['specimen']}
        raw = '标本甲；' + label + '；合成部位 ' + str(index)
        anchor = add_field(patient, report, kind + '.identity', entity, {'label': label, 'raw': label}, targets, raw=raw)
        review(patient, anchor)
        targets[kind.upper()] = anchor
        fact = add_field(patient, report, 'specimen.site' if kind == 'specimen' else 'assay.antibody', entity,
                         {'text': '合成部位 ' + str(index)}, targets, raw=raw)
        review(patient, fact)
        selected.extend([anchor, fact])
    chosen = selection(document, *selected)
    snapshot = build_snapshot(patient, chosen)
    scope_key = kind + '_scope'
    assert len({row['content']['semantic_qualifiers'][scope_key]['token'] for row in snapshot['clinical_fields']}) == 2
    shared = create_share(patient, patient.account, chosen).share
    scopes = [row['content']['semantic_qualifiers'][scope_key] for row in shared.snapshot['clinical_fields']]
    assert len({scope['token'] for scope in scopes}) == 2
    label = '选定标本' if kind == 'specimen' else '选定检测'
    assert {scope['label'] for scope in scopes} == {label + ' 1', label + ' 2'}
    for anchor, member in zip(selected[::2], selected[1::2]):
        same = [row['content']['semantic_qualifiers'][scope_key]['token']
                for row in shared.snapshot['clinical_fields'] if row['id'] in {str(anchor.pk), str(member.pk)}]
        assert len(same) == 2 and len(set(same)) == 1
    assert 'http://' not in json.dumps(shared.snapshot, ensure_ascii=False)
    assert_snapshot_current(patient, shared.snapshot)
