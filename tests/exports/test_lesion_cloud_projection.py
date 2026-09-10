"""Main's access omission must preserve selected lesion values and strict closure."""
from copy import deepcopy
import io
import json
import zipfile

import pytest

from apps.cloud_imaging.projection import OMITTED
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.facts.readmodels import effective_fact
from apps.lesions.readmodels import review_observations
from apps.lesions.services import create_lesion
from apps.patients.sharing_content import normalize_scope, project_snapshot
from tests.documents.fakes import InMemoryObjectStore
from tests.facts.test_scoped_laterality import confirm
from tests.lesions.factories import imaging_observation


pytestmark = pytest.mark.django_db
ACCESS_URL = 'https://images.example.invalid/view?key=LESION_SYNTHETIC_SECRET#entry'
ARRAYS = ('lesions', 'lesion_observations', 'lesion_measurements')


def projected_lesion(user_model, location='name', suffix='case'):
    patient, document, report = imaging_observation(
        user_model, name='lesion-cloud-' + location + '-' + suffix, confirmed=False, suv='3.2',
    )
    changed_key = {'site': 'lesion.site', 'dimensions': 'lesion.dimensions', 'suvmax': 'lesion.suvmax'}.get(location)
    for field in report.fields.all():
        if field.field_key == changed_key:
            value = deepcopy(field.automatic_content['value'])
            text_key = 'text' if location == 'site' else 'raw'
            value[text_key] += ' ' + ACCESS_URL
            confirm(patient, field, action='CORRECT', changes={'value': value, 'raw_value': value[text_key]})
        elif field.field_key != 'lesion.laterality':
            confirm(patient, field)
    observation = review_observations(patient, actor=patient.account)[0]
    create_lesion(patient, actor=patient.account, observation_id=observation['id'],
        expected_revision=observation['revision_number'], expected_source=observation['source_token'],
        name='观察 ' + ACCESS_URL if location == 'name' else '观察 <A>', checked_original=True)
    lesion = patient.lesions.get()
    scope = {'mode': 'documents', 'document_ids': [str(document.pk)], 'lesion_ids': [str(lesion.pk)],
        'clinical_field_ids': [str(f.pk) for f in report.fields.filter(field_key__in=(
            'lesion.site', 'lesion.dimensions', 'lesion.suvmax'))], 'sections': ['imaging'], 'details': True}
    return patient, document, report, scope


def private_values(patient, document):
    return {
        'facts': list(document.facts.order_by('pk').values('pk', 'automatic_content', 'revision_number', 'raw_text')),
        'revisions': list(document.facts.order_by('pk').values('revisions__id', 'revisions__before', 'revisions__after')),
        'lesions': list(patient.lesions.order_by('pk').values()),
    }


@pytest.mark.parametrize('location', ['name', 'site', 'dimensions', 'suvmax'])
def test_access_omission_preserves_selected_graph_round_trip_and_private_values(django_user_model, location):
    patient, document, _, scope = projected_lesion(django_user_model, location)
    before = private_values(patient, document)
    snapshot = build_snapshot(patient, scope)
    assert_snapshot_current(patient, snapshot)
    payload = json_bytes(snapshot)
    assert ACCESS_URL not in payload.decode() and 'LESION_SYNTHETIC_SECRET' not in payload.decode()
    assert OMITTED in payload.decode()
    data = read_structured_data(payload)
    assert all(data[key] == snapshot[key] for key in ARRAYS)
    assert {p['value'] for p in data['lesion_measurements']} == {'12', '3.2'}
    assert all(p['date_value'] is None and p['method'] == ['', ''] for p in data['lesion_measurements'])
    shared = project_snapshot(snapshot, normalize_scope(scope))
    public = json.dumps({key: shared[key] for key in ARRAYS}, ensure_ascii=False)
    assert 'LESION_SYNTHETIC_SECRET' not in public
    assert {p['value'] for p in shared['lesion_measurements']} == {'12', '3.2'}
    tables = csv_tables(snapshot)
    assert 'LESION_SYNTHETIC_SECRET' not in '\n'.join(p.decode('utf-8-sig') for p in tables.values())
    artifact = build_artifact(snapshot, {'format': 'zip', 'parts': ['json', 'csv']}, InMemoryObjectStore())
    with zipfile.ZipFile(io.BytesIO(artifact.payload)) as bundle:
        assert read_structured_data(bundle.read('records.json'))['lesion_measurements'] == data['lesion_measurements']
    assert private_values(patient, document) == before


@pytest.mark.parametrize('corruption', ['false', 'string', 'no_marker', 'extra_key', 'wrong_number'])
def test_omission_metadata_does_not_relax_relationship_or_value_validation(django_user_model, corruption):
    patient, _, _, scope = projected_lesion(django_user_model, 'name', corruption)
    data = json.loads(json_bytes(build_snapshot(patient, scope)))
    if corruption == 'false':
        data['lesions'][0]['external_access_omitted'] = False
    elif corruption == 'string':
        data['lesions'][0]['external_access_omitted'] = 'true'
    elif corruption == 'no_marker':
        data['lesions'][0]['name'] = '未发生省略'
    elif corruption == 'extra_key':
        data['lesions'][0]['unselected_source_body'] = '不允许新增的正文'
    else:
        data['lesion_measurements'][0]['value'] = '999'
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


def test_hidden_url_only_revision_still_invalidates_identical_public_omission(django_user_model):
    patient, _, report, scope = projected_lesion(django_user_model, 'dimensions', 'private-change')
    snapshot = build_snapshot(patient, scope)
    field = report.fields.get(field_key='lesion.dimensions')
    value = deepcopy(effective_fact(field)['content']['value'])
    value['raw'] = value['raw'].replace('LESION_SYNTHETIC_SECRET', 'ANOTHER_SYNTHETIC_SECRET')
    confirm(patient, field, action='CORRECT', changes={'value': value, 'raw_value': value['raw']})
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)
