"""The published cloud format and new cancer selection remain independent."""
from copy import deepcopy
import io
import json
import zipfile

import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import build_artifact, json_bytes, read_structured_data
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_services import _collect, _row, _select
from tests.cloud_imaging.test_controlled_open import confirmed
from tests.cloud_imaging.test_selected_output import source_selection
from tests.cloud_imaging.test_source_services import FIRST_URL, SECOND_URL, _decide
from tests.documents.fakes import InMemoryObjectStore

pytestmark = pytest.mark.django_db


def mixed(django_user_model):
    _, patient, _, cloud = confirmed(django_user_model)
    _collect(patient)
    selection = selected_body(patient, cancer_candidate_ids=[_row(patient)['id']],
                              include_indicator_ordering=True, cloud_source_ids=[str(cloud.pk)])
    return patient, cloud, selection


def test_mixed_zip_and_share_retain_both_explicit_scopes_without_originals(django_user_model):
    patient, cloud, selection = mixed(django_user_model)
    snapshot = build_snapshot(patient, selection)
    assert_snapshot_current(patient, snapshot)
    public = read_structured_data(json_bytes(snapshot))
    assert public['schema_version'] == '1.6'
    assert len(public['cancer_candidates']) == len(public['indicator_ordering']) == 1
    assert public['cloud_imaging_sources'][0]['current_url'] == FIRST_URL
    assert public['documents'] == []
    artifact = build_artifact(snapshot, {'format': 'zip', 'parts': ['json', 'csv', 'pdf']}, InMemoryObjectStore())
    try:
        with zipfile.ZipFile(artifact.stream) as bundle:
            manifest = json.loads(bundle.read('manifest.json'))
            assert manifest['cloud_source_ids'] == [str(cloud.pk)]
            assert manifest['cancer_candidate_ids'] == selection['cancer_candidate_ids']
            assert manifest['include_indicator_ordering'] is True
            assert not any(name.startswith('originals/') for name in bundle.namelist())
            assert read_structured_data(bundle.read('records.json')) == public
    finally:
        artifact.close()
    share = create_share(patient, patient.account, selection).share
    assert share.snapshot['documents'] == []
    assert len(share.snapshot['cancer_candidates']) == len(share.snapshot['cloud_imaging_sources']) == 1
    assert share.cloud_sources.count() == 1


@pytest.mark.parametrize('changed', ['cloud', 'ordering'])
def test_either_selected_domain_change_invalidates_mixed_snapshot(django_user_model, changed):
    patient, cloud, selection = mixed(django_user_model)
    snapshot = build_snapshot(patient, selection)
    if changed == 'cloud':
        _decide(patient, cloud, 'CORRECT', changes={'url': SECOND_URL})
    else:
        _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)


def test_published_cloud_15_without_cancer_arrays_still_reads_unchanged(django_user_model):
    _, patient, _, source = confirmed(django_user_model)
    data = json.loads(json_bytes(build_snapshot(patient, source_selection(source))))
    data['schema_version'] = '1.5'
    del data['cancer_candidates'], data['indicator_ordering']
    before = deepcopy(data)
    result = read_structured_data(json.dumps(data))
    assert result['cloud_imaging_sources'] == data['cloud_imaging_sources']
    assert result['cancer_candidates'] == result['indicator_ordering'] == []
    assert data == before


def test_legacy_cloud_version_cannot_hide_new_cancer_arrays(django_user_model):
    patient, _, selection = mixed(django_user_model)
    data = json.loads(json_bytes(build_snapshot(patient, selection)))
    data['schema_version'] = '1.5'
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))
