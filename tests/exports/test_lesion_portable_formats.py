from copy import deepcopy
import csv
import io
import json
import zipfile

import pytest

from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import card_sections
from apps.patients.sharing_content import normalize_scope, project_snapshot
from tests.documents.fakes import InMemoryObjectStore
from tests.exports.test_lesion_selected_material import pair, ids, selection


pytestmark = pytest.mark.django_db
ARRAYS = ('lesions', 'lesion_observations', 'lesion_measurements')


def prepared(user_model, name='lesion-portable'):
    patient, documents, reports, lesion = pair(user_model, name)
    chosen = [identity for report in reports for identity in ids(report, 'lesion.site', 'lesion.dimensions',
                                                                 'lesion.suvmax', 'report.exam_date', 'imaging.modality')]
    scope = selection(documents, lesion, chosen)
    return patient, reports, build_snapshot(patient, scope), scope


def test_json_csv_zip_and_card_use_one_selected_graph_with_exact_values(django_user_model):
    _, _, snapshot, _ = prepared(django_user_model)
    payload = json_bytes(snapshot)
    data = read_structured_data(payload)
    assert data['schema_version'] == '1.7'
    assert all(data[key] == snapshot[key] for key in ARRAYS)
    assert not any(word in payload.decode() for word in ('lesion_fingerprint', 'lesion_binding_ids', 'source_binding'))
    tables = csv_tables(snapshot)
    assert all(key + '.csv' in tables for key in ARRAYS)
    names = list(csv.DictReader(io.StringIO(tables['lesions.csv'].decode('utf-8-sig'))))
    assert names[0]['name'] == "'=选定观察 <A>" and data['lesions'][0]['name'] == '=选定观察 <A>'
    entries = [entry['text'] for section in card_sections(snapshot) if section['key'] == 'lesions' for entry in section['entries']]
    assert any('人工确认的观察分组' in text for text in entries)
    assert any('1.25' in text and '12.5' in text and '10' in text for text in entries)
    assert any('2.5' in text and '差值' in text for text in entries)
    bundle = build_artifact(snapshot, {'format': 'zip', 'parts': ['json', 'csv']}, InMemoryObjectStore())
    with zipfile.ZipFile(io.BytesIO(bundle.payload)) as archive:
        assert json.loads(archive.read('records.json'))['lesion_measurements'] == data['lesion_measurements']
        assert archive.read('csv/lesions.csv') == tables['lesions.csv']


@pytest.mark.parametrize('corruption', ['wrong_field', 'wrong_value', 'wrong_context', 'whole_scope', 'missing_table'])
def test_reader_rejects_dangling_or_contradictory_new_graph_data(django_user_model, corruption):
    _, reports, snapshot, _ = prepared(django_user_model, 'lesion-portable-invalid')
    data = json.loads(json_bytes(snapshot))
    if corruption == 'wrong_field':
        data['lesion_measurements'][0]['field_id'] = ids(reports[0], 'lesion.site')[0]
    elif corruption == 'wrong_value':
        data['lesion_measurements'][0]['value'] = '999'
    elif corruption == 'wrong_context':
        data['lesion_measurements'][0]['context_field_ids'] = []
    elif corruption == 'whole_scope':
        data['lesion_observations'][0]['laterality_scope']['scope_state'] = 'WHOLE_ENTITY'
    else:
        data.pop('lesion_measurements')
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


@pytest.mark.parametrize('version', ['1.0', '1.1', '1.2', '1.3', '1.4', '1.5'])
def test_all_published_formats_keep_existing_data_with_empty_new_arrays(version):
    data = {'schema_version': version, 'documents': [], 'facts': [{'id': 'retained-old'}], 'labs': [], 'sources': [],
            'clinical_reports': [], 'clinical_fields': [], 'clinical_field_sources': [], 'self_records': [],
            'glucose_records': [], 'glucose_record_sources': [],
            'cloud_imaging_sources': [], 'cloud_imaging_evidence': []}
    from apps.exports.treatment import ARRAYS as OLD_ARRAYS
    data.update({key: [] for key in OLD_ARRAYS})
    original = deepcopy(data)
    result = read_structured_data(json.dumps(data))
    assert all(result[key] == original[key] for key in original)
    assert all(result[key] == [] for key in ARRAYS)


def test_published_cloud_format_keeps_selected_source_and_defaults_only_new_lesion_tables(django_user_model):
    from tests.cloud_imaging.test_controlled_open import confirmed
    from tests.cloud_imaging.test_selected_output import source_selection
    from tests.cloud_imaging.test_source_services import FIRST_URL
    _, patient, _, source = confirmed(django_user_model)
    data = json.loads(json_bytes(build_snapshot(patient, source_selection(source))))
    # Explicit legacy 1.5 shape: cloud tables exist, lesion tables do not.
    data['schema_version'] = '1.5'
    for key in ARRAYS:
        assert data.pop(key) == []
    encoded = json.dumps(data)
    restored = read_structured_data(encoded)
    assert all(restored[key] == value for key, value in data.items())
    assert restored['cloud_imaging_sources'][0]['current_url'] == FIRST_URL
    assert all(restored[key] == [] for key in ARRAYS)
    assert json.dumps(data) == encoded


def test_lesion_graph_cannot_claim_the_previously_published_cloud_only_version(django_user_model):
    _, _, snapshot, _ = prepared(django_user_model, 'lesion-version-downgrade')
    data = json.loads(json_bytes(snapshot))
    data['schema_version'] = '1.5'
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


def test_share_reprojects_context_from_actual_selected_fields_and_forbids_originals(django_user_model):
    _, reports, snapshot, scope = prepared(django_user_model, 'lesion-share-field-intersection')
    scope['clinical_field_ids'] = ids(reports[1], 'lesion.site', 'lesion.dimensions')
    normalized = normalize_scope(scope)
    assert normalized['lesion_ids'] == scope['lesion_ids']
    shared = project_snapshot(snapshot, normalized)
    assert len(shared['lesion_observations']) == len(shared['lesion_measurements']) == 1
    point = shared['lesion_measurements'][0]
    assert point['date_value'] is None and point['method'] == ['', ''] and point['context_field_ids'] == []
    assert 'source_binding' not in json.dumps({key: shared[key] for key in ARRAYS})
    with pytest.raises(ExportInputError):
        normalize_scope({**scope, 'sections': ['imaging', 'sources']})
    with pytest.raises(ExportInputError):
        normalize_scope({**scope, 'sections': ['labs']})
