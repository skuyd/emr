"""Stored text limits and projected URL expansions are different contracts."""
from copy import deepcopy
import json

import pytest

from apps.cloud_imaging.projection import ACCESS_STRING, OMITTED
from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import json_bytes, read_structured_data
from apps.facts.readmodels import effective_fact
from apps.lesions.readmodels import lesion_state, review_observations
from apps.lesions.services import create_lesion, rename_lesion
from tests.exports.test_lesion_cloud_projection import projected_lesion, private_values
from tests.facts.test_scoped_laterality import confirm
from tests.lesions.factories import imaging_observation


pytestmark = pytest.mark.django_db


def limit_text(limit, suffix, prefix=''):
    return prefix + '合' * (limit - len(prefix) - len(suffix) - 1) + ' ' + suffix


@pytest.mark.parametrize('suffix', [
    'http://a.b', 'https://a.b', 'http://a', 'http://a http://b',
    OMITTED + ' http://a', OMITTED,
])
def test_saved_maximum_name_keeps_every_character_after_output_omission(django_user_model, suffix):
    patient, document, _, selection = projected_lesion(django_user_model, 'name', 'maximum-name')
    lesion = patient.lesions.get()
    name = limit_text(120, suffix)
    rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                  expected_revision=lesion.revision_number, name=name)
    lesion.refresh_from_db()
    assert len(name) == 120 and lesion_state(lesion)['name'] == name
    before = private_values(patient, document)
    snapshot = build_snapshot(patient, selection)
    expected = ACCESS_STRING.sub(OMITTED, name)
    assert snapshot['lesions'][0]['name'] == expected
    assert (snapshot['lesions'][0].get('external_access_omitted') is True) == ('http' in suffix)
    data = read_structured_data(json_bytes(snapshot))
    assert data['lesions'][0]['name'] == expected
    assert data['lesion_measurements'] == snapshot['lesion_measurements']
    assert private_values(patient, document) == before


@pytest.mark.parametrize('mutation', [
    'false_flag', 'string_flag', 'no_flag', 'only_ancestor_flag',
    'no_marker', 'over_expansion_budget', 'ordinary_over_limit',
])
def test_unproven_or_unbounded_name_expansion_remains_invalid(django_user_model, mutation):
    patient, _, _, selection = projected_lesion(django_user_model, 'name', mutation)
    data = json.loads(json_bytes(build_snapshot(patient, selection)))
    row = data['lesions'][0]
    row['name'] = '合' * 112 + OMITTED  # 123 display chars, minimum pre-omission length 120.
    assert len(row['name']) == 123
    if mutation == 'false_flag':
        row['external_access_omitted'] = False
    elif mutation == 'string_flag':
        row['external_access_omitted'] = 'true'
    elif mutation in {'no_flag', 'only_ancestor_flag'}:
        row.pop('external_access_omitted')
        if mutation == 'only_ancestor_flag':
            data['external_access_omitted'] = True
    elif mutation == 'no_marker':
        row['name'] = '合' * 121
    elif mutation == 'over_expansion_budget':
        row['name'] = '合' * 113 + OMITTED
    else:
        row['name'] = '合' * 121
        row.pop('external_access_omitted')
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


def long_typed_projection(user_model, key, text_key, maximum, *, role=None):
    patient, document, report = imaging_observation(user_model, name='bounded-typed-value', confirmed=False, suv='3.2')
    changed = None
    original = limit_text(maximum, 'http://a http://b', '左肺 ' if key == 'lesion.site' else '')
    for field in report.fields.all():
        if field.field_key == key:
            value = deepcopy(field.automatic_content['value'])
            value[text_key] = original
            if role is not None:
                value['measurement_role'] = role
            confirm(patient, field, action='CORRECT', changes={'value': value, 'raw_value': original})
            changed = field
        elif field.field_key not in {'lesion.laterality', 'lesion.scoped_laterality'}:
            confirm(patient, field)
    assert changed is not None
    current = review_observations(patient, actor=patient.account)[0]
    create_lesion(patient, actor=patient.account, observation_id=current['id'],
        expected_revision=current['revision_number'], expected_source=current['source_token'],
        name='合成观察', checked_original=True)
    selection = {'mode': 'documents', 'document_ids': [str(document.pk)],
        'lesion_ids': [str(patient.lesions.get().pk)], 'sections': ['imaging'], 'details': True,
        'clinical_field_ids': [str(field.pk) for field in report.fields.filter(field_key__in=(
            'lesion.site', 'lesion.dimensions', 'lesion.suvmax', key))]}
    return patient, document, changed, selection, original


@pytest.mark.parametrize(('key', 'text_key', 'maximum'), [
    ('lesion.site', 'text', 30000),
    ('imaging.modality', 'raw', 512),
    ('lesion.dimensions', 'raw', 512),
    ('lesion.suvmax', 'raw', 512),
    ('lesion.suvmax', 'unit', 30),
])
def test_confirmed_bounded_typed_strings_survive_multiple_short_url_expansions(django_user_model, key, text_key, maximum):
    patient, document, field, selection, original = long_typed_projection(django_user_model, key, text_key, maximum)
    assert len(original) == maximum
    assert effective_fact(field)['content']['value'][text_key] == original
    before = private_values(patient, document)
    snapshot = build_snapshot(patient, selection)
    data = read_structured_data(json_bytes(snapshot))
    selected = next(row for row in data['clinical_fields'] if row['id'] == str(field.pk))
    expected = original.replace('http://a', OMITTED).replace('http://b', OMITTED)
    assert selected['content']['value'][text_key] == expected
    assert len(expected) == maximum + 6
    assert data['lesion_measurements'] == snapshot['lesion_measurements']
    assert private_values(patient, document) == before


def test_redactor_minimum_url_match_is_eight_characters():
    assert ACCESS_STRING.fullmatch('http://a') and len('http://a') == 8
    assert not ACCESS_STRING.fullmatch('http://')


@pytest.mark.parametrize('text_key', ['site_text', 'raw'])
def test_scoped_typed_portable_member_length_is_not_a_whole_side_or_source_attestation(django_user_model, text_key):
    from apps.cloud_imaging.projection import project_default_snapshot
    from apps.facts.clinical_schema import field_content
    from tests.exports.test_laterality_portable_scope import scoped_data

    # This is a synthetic portable-shape case, not a new stored source binding.
    data, field, _ = scoped_data(django_user_model)
    value = deepcopy(field['content']['value'])
    # Delimit the URL from the following system-generated side suffix. The
    # separate unseparated-display diagnostic is preserved in private evidence.
    original = limit_text(512, 'http://a http://b ', '左肺 ')
    value['members'][0][text_key] = original
    field['content'] = field_content('lesion.scoped_laterality', value, original)
    projected = project_default_snapshot(data)
    result = read_structured_data(json.dumps(projected))
    item = next(row for row in result['clinical_fields'] if row['id'] == field['id'])
    assert item['content']['value']['members'][0][text_key] == original.replace('http://a', OMITTED).replace('http://b', OMITTED)
    assert item['laterality_scope'] == field['laterality_scope']
    assert item['laterality_scope']['scope_state'] == 'NAMED_MEMBERS_ONLY'
