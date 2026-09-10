"""Derive display from selected typed values after access strings are omitted."""
from copy import deepcopy
import json

import pytest

from apps.cloud_imaging.projection import OMITTED, project_default_snapshot
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import SnapshotChanged
from apps.exports.formats import json_bytes, read_structured_data
from apps.facts.clinical_schema import field_content
from apps.facts.laterality_services import add_laterality_scope
from apps.facts.readmodels import effective_fact
from tests.exports.test_lesion_cloud_projection import private_values, projected_lesion
from tests.exports.test_laterality_portable_scope import scoped_data
from tests.facts.test_scoped_laterality import confirm, named_value
from tests.lesions.factories import imaging_observation


@pytest.mark.django_db
def test_real_confirmed_member_with_terminal_url_keeps_its_static_side_in_json(django_user_model):
    patient, document, report = imaging_observation(
        django_user_model, name='url-member-display', site='左肺 http://a ', confirmed=False)
    parent = report.fields.get(field_key='lesion.site')
    site = '左肺 http://a'
    confirm(patient, parent, action='CORRECT', changes={'value': {'text': site}, 'raw_value': site})
    fragment = next(part for part in parent.source_fragments.select_related('ocr_block') if site in part.raw_text)
    start = fragment.ocr_block.text.index(site)
    operation = add_laterality_scope(patient, actor=patient.account, parent_id=parent.pk,
        expected_parent_revision=parent.revisions.count(),
        expected_parent_source=effective_fact(parent)['current_source_token'],
        scope_kind='NAMED_MEMBERS_ONLY', value=named_value((site, 'LEFT')),
        ranges=[{'member_key': 'member:001', 'parent_fragment_id': fragment.pk,
                 'start_offset': start, 'end_offset': start + len(site)}],
        checked_original=True, confirm=True)
    child = operation.new_fact
    assert effective_fact(child)['usable'] and effective_fact(child)['source_valid']
    before = private_values(patient, document)
    selection = {'mode': 'documents', 'document_ids': [str(document.pk)], 'sections': ['imaging'],
                 'clinical_field_ids': [str(parent.pk), str(child.pk)]}
    snapshot = build_snapshot(patient, selection)
    assert_snapshot_current(patient, snapshot)
    data = read_structured_data(json_bytes(snapshot))
    row = next(item for item in data['clinical_fields'] if item['id'] == str(child.pk))
    assert row['content']['text'] == f'部位组内的原文侧别：仅限列明部位：左肺 {OMITTED}（左侧）'
    assert row['content']['value']['members'][0]['code'] == 'LEFT'
    assert row['laterality_scope']['scope_state'] == 'NAMED_MEMBERS_ONLY'
    assert project_default_snapshot(snapshot) == snapshot
    assert private_values(patient, document) == before


@pytest.mark.django_db
@pytest.mark.parametrize('change', [
    'legacy_location', 'nested_location', 'wrong_field_key', 'wrong_schema',
    'wrong_type', 'wrong_result', 'wrong_display', 'wrong_value_shape',
    'unconfirmed', 'missing_row_identity',
])
def test_arbitrary_or_contradictory_content_does_not_acquire_a_typed_display(django_user_model, change):
    data, field, _ = scoped_data(django_user_model)
    value = deepcopy(field['content']['value'])
    value['members'][0]['site_text'] = '左肺 http://a'
    value['members'][0]['raw'] = '左肺 http://a'
    value['members'][0]['code'] = 'LEFT'
    field['content'] = field_content('lesion.scoped_laterality', value, '左肺 http://a')
    if change == 'legacy_location':
        data = {'facts': [field]}
    elif change == 'nested_location':
        data = {'notes': data}
    elif change == 'wrong_field_key':
        field['field_key'] = 'lesion.site'
    elif change == 'wrong_schema':
        field['content']['schema_version'] = 'unrecognized'
    elif change == 'wrong_type':
        field['content']['value_type'] = 'TEXT'
    elif change == 'wrong_result':
        field['content']['result_type'] = 'INFERRED'
    elif change == 'wrong_display':
        field['content']['text'] = '非规范展示 http://a（不能附加源结论）'
    elif change == 'unconfirmed':
        field['status'] = 'PENDING'
    elif change == 'missing_row_identity':
        field.pop('id')
    else:
        field['content']['value']['unselected_context'] = '禁止附加的字段'
    before = deepcopy(data)
    projected = project_default_snapshot(data)
    text = json.dumps(projected, ensure_ascii=False)
    assert 'http://' not in text and '（左侧）' not in text
    assert data == before


@pytest.mark.django_db
@pytest.mark.parametrize('key', ['lesion.dimensions', 'lesion.suvmax'])
def test_typed_historical_role_survives_omission_at_the_last_value_character(django_user_model, key):
    patient, _, _, selection = projected_lesion(django_user_model, 'dimensions', 'historical-display')
    data = json.loads(json_bytes(build_snapshot(patient, selection)))
    field = next(row for row in data['clinical_fields'] if row['field_key'] == key)
    value = deepcopy(field['content']['value'])
    value.pop('external_access_omitted', None)
    value['measurement_role'] = 'HISTORICAL'
    value['raw' if key == 'lesion.dimensions' else 'unit'] = '12mm http://a' if key == 'lesion.dimensions' else 'http://a'
    field['content'] = field_content(key, value, value['raw'])
    before = deepcopy(data)
    projected = project_default_snapshot(data)
    row = next(item for item in projected['clinical_fields'] if item['id'] == field['id'])
    expected = ('病灶尺寸：12mm ' if key == 'lesion.dimensions' else '原文 SUVmax：3.2 ') + OMITTED + '（历史记录值）'
    assert row['content']['text'] == expected
    assert row['content']['value']['measurement_role'] == 'HISTORICAL'
    assert data == before


@pytest.mark.django_db
def test_pre_repair_output_rule_snapshot_requires_regeneration(django_user_model, monkeypatch):
    from apps.cloud_imaging import projection
    from tests.exports.test_lesion_omission_length import long_typed_projection

    patient, document, field, selection, original = long_typed_projection(
        django_user_model, 'lesion.dimensions', 'raw', 512, role='HISTORICAL')
    before = private_values(patient, document)
    assert effective_fact(field)['content']['value']['measurement_role'] == 'HISTORICAL'
    with monkeypatch.context() as previous:
        previous.setattr(projection, 'PROJECTION_RULE', 'cloud-access-omission-v1')
        old_snapshot = build_snapshot(patient, selection)
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, old_snapshot)
    fresh = build_snapshot(patient, selection)
    assert_snapshot_current(patient, fresh)
    assert next(row for row in fresh['lesion_measurements'] if row['field_id'] == str(field.pk))['role'] == 'HISTORICAL'
    assert effective_fact(field)['content']['value']['raw'] == original
    assert private_values(patient, document) == before
