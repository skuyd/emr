"""Actual published lesion/cloud scope composes with explicit cancer output."""
from copy import deepcopy
import json
import zipfile
from uuid import uuid4

import pytest

from apps.cloud_imaging.readmodels import document_snapshot
from apps.cloud_imaging.services import add_manual_source
from apps.exports.content import build_snapshot
from apps.exports.errors import ExportInputError, ExportUnavailable
from apps.exports.formats import build_artifact, json_bytes, read_structured_data
from apps.exports.services import create_preview, get_preview
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_services import _collect, _row, _select
from tests.cloud_imaging.test_source_services import FIRST_URL, _decide
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_lesion_output_bindings import authenticated
from tests.exports.test_lesion_selected_material import pair, ids, selection
from tests.patients.test_family_shares import exchange

pytestmark = pytest.mark.django_db
CANCER_ARRAYS = ('cancer_candidates', 'indicator_ordering')
LESION_ARRAYS = ('lesions', 'lesion_observations', 'lesion_measurements')


def mixed_selected(user_model):
    patient, documents, reports, lesion = pair(user_model, 'cancer-lesion-cloud')
    original = document_snapshot(patient, actor=patient.account, document_id=documents[0].pk)
    cloud = add_manual_source(patient, actor=patient.account, document_id=documents[0].pk,
        page_id=documents[0].pages.first().pk, report_id=reports[0].pk, url=FIRST_URL,
        expected_source=original['input_token'], operation_id=uuid4())
    cloud = _decide(patient, cloud, 'CONFIRM')
    _collect(patient)
    fields = [pk for report in reports for pk in ids(report, 'lesion.site', 'lesion.dimensions',
                                                    'report.exam_date', 'imaging.modality')]
    scope = selected_body(patient, **selection(documents, lesion, fields),
                          cancer_candidate_ids=[_row(patient)['id']], include_indicator_ordering=True,
                          cloud_source_ids=[str(cloud.pk)])
    scope['sections'] = ['imaging', 'cancer_ordering', 'cloud_imaging']
    return patient, reports, lesion, cloud, scope


def test_three_domains_real_zip_and_share_preserve_independent_meanings(django_user_model):
    patient, reports, lesion, cloud, scope = mixed_selected(django_user_model)
    snapshot = build_snapshot(patient, scope)
    data = read_structured_data(json_bytes(snapshot))
    assert data['schema_version'] == '1.7'
    assert all(data[key] for key in (*CANCER_ARRAYS, *LESION_ARRAYS, 'cloud_imaging_sources', 'cloud_imaging_evidence'))
    assert data['cancer_candidates'][0]['source']['state'] == 'OMITTED'
    assert data['cloud_imaging_sources'][0]['current_url'] == FIRST_URL
    artifact = build_artifact(snapshot, {'format': 'zip', 'parts': ['json', 'csv', 'pdf']}, InMemoryObjectStore())
    try:
        with zipfile.ZipFile(artifact.stream) as archive:
            assert read_structured_data(archive.read('records.json')) == data
            assert all('csv/' + key + '.csv' in archive.namelist() for key in (*CANCER_ARRAYS, *LESION_ARRAYS))
            assert not any(name.startswith('originals/') for name in archive.namelist())
            manifest = json.loads(archive.read('manifest.json'))
            assert data['scope']['lesion_ids'] == [str(lesion.pk)]
            assert manifest['cancer_candidate_ids'] == scope['cancer_candidate_ids']
            assert manifest['cloud_source_ids'] == [str(cloud.pk)]
    finally:
        artifact.close()
    made = create_share(patient, patient.account, scope)
    assert all(made.share.snapshot[key] for key in (*CANCER_ARRAYS, *LESION_ARRAYS, 'cloud_imaging_sources'))
    assert FIRST_URL not in json.dumps(made.share.snapshot)
    assert made.share.cloud_sources.get().source_id == cloud.pk and made.share.lesion_sources.exists()


def test_published_16_keeps_nonempty_lesion_and_cloud_arrays_without_cancer(django_user_model):
    patient, _, _, _, scope = mixed_selected(django_user_model)
    scope.update(cancer_candidate_ids=[], include_indicator_ordering=False)
    data = json.loads(json_bytes(build_snapshot(patient, scope)))
    data['schema_version'] = '1.6'
    for key in CANCER_ARRAYS:
        assert data.pop(key) == []
    original = deepcopy(data)
    restored = read_structured_data(json.dumps(data))
    assert all(restored[key] == original[key] and restored[key] for key in (*LESION_ARRAYS, 'cloud_imaging_sources'))
    assert all(restored[key] == [] for key in CANCER_ARRAYS) and data == original


@pytest.mark.parametrize('key', CANCER_ARRAYS)
def test_published_16_rejects_each_nonempty_new_cancer_array(django_user_model, key):
    patient, _, _, _, scope = mixed_selected(django_user_model)
    data = json.loads(json_bytes(build_snapshot(patient, scope)))
    data['schema_version'] = '1.6'
    for other in CANCER_ARRAYS:
        if other != key:
            data[other] = []
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


@pytest.mark.parametrize('domain', ['cancer', 'lesion', 'cloud'])
def test_any_domain_loss_scrubs_whole_mixed_preview_and_share(django_user_model, domain):
    patient, _, _, _, scope = mixed_selected(django_user_model)
    client = authenticated(patient)
    job = create_preview(patient, client.session.session_key, scope, actor=patient.account)
    made = create_share(patient, patient.account, scope)
    reader, _ = _patient(django_user_model, 'mixed-domain-recipient')
    identity = exchange(reader, made.token)
    if domain == 'cancer':
        _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
    else:
        getattr(job, domain + '_sources').all().delete()
        getattr(made.share, domain + '_sources').all().delete()
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=patient.account)
    response = reader.get(f'/shared/{identity}/')
    assert response.status_code == 410 and FIRST_URL.encode() not in response.content
    job.refresh_from_db(); made.share.refresh_from_db()
    assert job.snapshot == made.share.snapshot == {}
    assert not job.cloud_sources.exists() and not made.share.cloud_sources.exists()


@pytest.mark.parametrize('kind', ['export', 'share'])
def test_both_real_forms_preserve_all_three_explicit_selections(django_user_model, kind):
    from apps.exports.forms import SelectionForm
    from apps.patients.share_forms import ShareForm
    patient, _, lesion, cloud, scope = mixed_selected(django_user_model)
    cls = SelectionForm if kind == 'export' else ShareForm
    empty = cls(patient, actor=patient.account)
    value = next(value for value, _ in empty.fields['cloud_source_ids'].choices if value.startswith(str(cloud.pk) + ':'))
    form = cls(patient, {**scope, 'nickname': patient.display_name, 'custom_clinical_fields': 'on',
                         'cloud_source_ids': [value]}, actor=patient.account)
    assert form.is_valid(), form.errors
    selected = form.selection()
    assert selected['lesion_ids'] == [str(lesion.pk)]
    assert selected['cloud_source_ids'] == [str(cloud.pk)]
    assert selected['cancer_candidate_ids'] == scope['cancer_candidate_ids']
    output = (build_snapshot(patient, selected) if kind == 'export'
              else create_share(patient, patient.account, selected).share.snapshot)
    assert all(output[key] for key in (*CANCER_ARRAYS, *LESION_ARRAYS, 'cloud_imaging_sources'))
