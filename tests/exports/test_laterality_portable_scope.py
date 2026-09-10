import json

import pytest

from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import json_bytes, read_structured_data
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_scoped_laterality import confirm


pytestmark = pytest.mark.django_db


def scoped_data(user_model):
    patient, report, parent, child = fixture(user_model, 'portable-scoped-contract')
    confirm(patient, parent)
    confirm(patient, child)
    snapshot = build_snapshot(patient, {'document_ids': [str(report.document_id)],
        'mode': 'documents', 'clinical_field_ids': [str(parent.pk), str(child.pk)], 'sections': ['imaging']})
    data = json.loads(json_bytes(snapshot))
    return data, next(row for row in data['clinical_fields'] if row['id'] == str(child.pk)), parent


def test_named_members_and_selected_parent_remain_one_explicit_portable_scope(django_user_model):
    data, field, parent = scoped_data(django_user_model)
    result = read_structured_data(json.dumps(data))
    assert result['clinical_fields'] == data['clinical_fields']
    assert field['laterality_scope'] == {'scope_state': 'NAMED_MEMBERS_ONLY',
        'parent_selected': True, 'parent_field_id': str(parent.pk)}
    assert field['content']['value_type'] == 'SCOPED_LATERALITY'


@pytest.mark.parametrize('corruption', ['missing_scope', 'whole_scope', 'wrong_parent', 'contradictory_selection',
                                         'wrong_content_field', 'wrong_value_type', 'wrong_display'])
def test_reader_rejects_contradictory_scope_identity_or_display(django_user_model, corruption):
    data, field, _ = scoped_data(django_user_model)
    if corruption == 'missing_scope':
        field.pop('laterality_scope')
    elif corruption == 'whole_scope':
        field['laterality_scope']['scope_state'] = 'WHOLE_ENTITY'
    elif corruption == 'wrong_parent':
        field['laterality_scope']['parent_field_id'] = field['id']
    elif corruption == 'contradictory_selection':
        field['laterality_scope']['parent_selected'] = False
    elif corruption == 'wrong_content_field':
        field['content']['field_key'] = 'lesion.laterality'
    elif corruption == 'wrong_value_type':
        field['content']['value_type'] = 'CODED'
    else:
        field['content']['text'] = '整体双侧（与列明部位作用范围矛盾）'
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))
