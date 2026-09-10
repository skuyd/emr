"""Only persisted source roles may supply value/label windows to evaluation."""
from copy import deepcopy

import pytest

from apps.facts.models import Fact
from tests.facts.test_pathology_named_report_routing import named_report_rows
from tests.facts.test_pathology_split_metadata import split_rows
from tests.tools.test_pathology_source_mapping import panel, mapped, scores


pytestmark = pytest.mark.django_db


def test_new_persisted_table_roles_map_full_marker_and_actual_labels(django_user_model):
    document, _, fixed = panel(django_user_model, rows=named_report_rows())
    result = mapped(document, fixed)
    marker = next(i for i in result['pages'][0]['items'] if i['field_key'] == 'ihc.marker')
    assert [p['raw_text'] for p in marker['value_evidence']] == ['PD-L1蛋白表达水平']
    assert [p['raw_text'] for p in marker['label_evidence']] == ['检测项目']
    assert marker['value'] == {'code': 'PD_L1', 'label': 'PD-L1', 'raw': 'PD-L1'}
    for item in scores(result):
        assert item['mapping_diagnostics'] == []
        assert any(p['raw_text'] == 'PD-L1蛋白表达水平' for p in item['bindings']['MARKER']['proof_evidence'])
        assert any(p['raw_text'] == '检测结果' for p in item['label_evidence'])


def test_mapping_does_not_turn_legacy_undeclared_fragments_into_label_evidence(django_user_model):
    document, _, fixed = panel(django_user_model, rows=named_report_rows(), name='legacy-roles')
    for field in document.facts.filter(representation='FIELD'):
        content = deepcopy(field.automatic_content)
        content.pop('literal_source', None)
        Fact.objects.filter(pk=field.pk).update(automatic_content=content)
    before = list(document.facts.values_list('id', 'automatic_content'))
    result = mapped(document, fixed)
    assert all(i['label_evidence'] == [] for i in result['pages'][0]['items'])
    marker = next(i for i in result['pages'][0]['items'] if i['field_key'] == 'ihc.marker')
    assert [p['raw_text'] for p in marker['value_evidence']] == ['PD-L1']
    assert all(i['mapping_diagnostics'] == [] for i in result['pages'][0]['items'])
    assert list(document.facts.values_list('id', 'automatic_content')) == before


def test_mapping_split_cells_preserves_raw_date_and_role_without_synthesized_colon(django_user_model):
    document, _, fixed = panel(django_user_model, rows=split_rows(), name='mapping-split')
    result = mapped(document, fixed)
    date = next(i for i in result['pages'][0]['items'] if i['field_key'] == 'assay.received_date')
    assert date['value'] == {'value': '2032-06-02', 'precision': 'DAY'}
    assert [p['raw_text'] for p in date['value_evidence']] == ['２０３２年０６月０２日']
    assert [p['raw_text'] for p in date['label_evidence']] == ['样本接收日期']
    for item in result['pages'][0]['items']:
        assert item['mapping_diagnostics'] == []
        for proof in item['label_evidence'] + item['value_evidence']:
            original = fixed[0]['regions'][proof['region_index']]
            assert proof['raw_text'] == original['text'][proof['start_offset']:proof['end_offset']]
            assert proof['polygon'] == original['polygon']


@pytest.mark.parametrize('change', ['missing', 'ancestor', 'other_field', 'null'])
def test_invalid_declared_roles_never_fall_back_to_apparently_valid_prefix(django_user_model, change):
    document, _, fixed = panel(django_user_model, name='mapping-invalid-' + change)
    score = document.facts.filter(field_key='ihc.score').first()
    content = deepcopy(score.automatic_content)
    source = content['literal_source']
    if change == 'null':
        content['literal_source'] = None
    elif change == 'missing':
        source['value_fragment_ordinals'] = [999]
    elif change == 'ancestor':
        source['value_fragment_ordinals'] = content['entity_context']['bindings'][0]['proof_fragment_ordinals']
    else:
        other = document.facts.get(field_key='ihc.marker').source_fragments.last()
        source['value_fragment_ordinals'] = [other.pk]
    # Change is persisted, not an argument manufactured only for the adapter.
    Fact.objects.filter(pk=score.pk).update(automatic_content=content)
    result = mapped(document, fixed)
    item = next(i for i in scores(result) if i['candidate_id'] == str(score.pk))
    assert item['mapping_diagnostics'] and not item['value_evidence'] and not item['label_evidence']
    assert len(scores(result)) == 2
