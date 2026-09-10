"""New context semantics coexist with every already merged selected domain."""
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
import uuid
import zipfile

import pytest

from apps.exports.content import SCHEMA_VERSION, assert_snapshot_current, build_snapshot
from apps.exports.formats import build_artifact, json_bytes, read_structured_data
from apps.exports.treatment import ARRAYS as TREATMENT_ARRAYS
from apps.glucose.output import ARRAYS as GLUCOSE_ARRAYS
from apps.glucose.services import create_record as create_glucose
from apps.patients.sharing import create_share
from apps.self_records.services import create_record as create_daily
from tests.documents.fakes import InMemoryObjectStore
from tests.exports.test_pathology_exports import _graph, selection
from tests.facts.pathology_factories import add_field, confirm_graph, ihc_fixture, review
from tests.glucose.test_payloads import payload as glucose_payload
from tests.labs.test_trends import _observation
from tests.self_records.test_payloads import payload as daily_payload
from tests.treatments.test_manual_events import create as create_treatment


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('include_cloud',[False,True])
@pytest.mark.parametrize('include_molecular', [False, True])
def test_ihc_selection_preserves_actual_glucose_daily_lab_treatment_tables_and_roundtrip(django_user_model,include_cloud,include_molecular):
    _, patient, document, report, fields = _graph(django_user_model, "pathology-mixed-output")
    lab_document, lab = _observation(patient, date(2030, 2, 3), "4")
    daily = create_daily(patient, patient.account, daily_payload(), creation_key=uuid.uuid4()).record
    glucose = create_glucose(patient, patient.account, glucose_payload(), creation_key=uuid.uuid4()).record
    event = create_treatment(patient, patient.account)
    create_daily(patient, patient.account, daily_payload(notes="UNSELECTED_DAILY"), creation_key=uuid.uuid4())
    create_glucose(patient, patient.account, glucose_payload(notes="UNSELECTED_GLUCOSE"), creation_key=uuid.uuid4())
    scope = {**selection(document, fields["cps"]), "document_ids": [str(document.pk), str(lab_document.pk)],
             "self_record_ids": [str(daily.pk)], "glucose_record_ids": [str(glucose.pk)],
             "treatment_event_ids": [str(event.pk)], "observation_ids": [str(lab.pk)],
             "sections": ["imaging", "labs", "self_records", "glucose", "treatment"], "details": True}
    if include_cloud:
        from apps.cloud_imaging.readmodels import document_snapshot
        from apps.cloud_imaging.services import add_manual_source
        from tests.cloud_imaging.test_source_services import FIRST_URL, _decide
        original=document_snapshot(patient,actor=patient.account,document_id=document.pk)
        cloud=add_manual_source(patient,actor=patient.account,document_id=document.pk,page_id=document.pages.first().pk,
            report_id=report.pk,url=FIRST_URL,expected_source=original['input_token'],operation_id=uuid.uuid4())
        cloud=_decide(patient,cloud,'CONFIRM')
        scope['cloud_source_ids']=[str(cloud.pk)]
    expected_fields = {str(fields['cps'].pk)}
    if include_molecular:
        from apps.facts.clinical_services import create_manual_report
        from tests.documents.test_detail_viewer import _document
        from tests.facts.molecular_factories import add, variant_source
        from tests.facts.test_molecular_contracts import variant, quantity
        molecular_document, _ = _document(patient, page_count=1, status='PROCESSING_FAILED')
        molecular_report = create_manual_report(patient, actor=patient.account, document_id=molecular_document.pk,
            spans=[{'page_number': 1}], title='合成分子检测报告', expected_lifecycle_revision=0,
            expected_version_id=None, routing_kind='MOLECULAR')
        specimen = add(patient, molecular_report, 'specimen.identity', 'specimen:a', {'label': '标本甲', 'raw': '标本甲'}, {})
        assay = add(patient, molecular_report, 'assay.identity', 'assay:a', {'label': '检测甲', 'raw': '检测甲'}, {'SPECIMEN': specimen})
        targets = {'SPECIMEN': specimen, 'ASSAY': assay}
        identity = add(patient, molecular_report, 'variant.identity', 'variant:a', variant(), targets)
        metric = add(patient, molecular_report, 'variant.allele_fraction', 'variant:a', quantity(), {**targets, 'VARIANT': identity},
            raw='标本甲；检测甲；' + variant_source() + '；01.20 %')
        for fact in (specimen, assay, identity, metric):
            review(patient, fact)
        scope['document_ids'].append(str(molecular_document.pk))
        scope['clinical_field_ids'].append(str(metric.pk))
        expected_fields.add(str(metric.pk))
    snapshot = build_snapshot(patient, scope)
    data = json.loads(json_bytes(snapshot))
    assert data["schema_version"] == SCHEMA_VERSION
    assert {row['id'] for row in data['clinical_fields']} == expected_fields
    assert data["cancer_candidates"] == []
    assert data["indicator_ordering"] == []
    assert all(data[key] == [] for key in ("lesions", "lesion_observations", "lesion_measurements"))
    identities = (("labs", lab.pk), ("self_records", daily.pk),
                  ("glucose_records", glucose.pk), ("treatment_events", event.pk))
    for key, expected in identities:
        assert [row["id"] for row in data[key]] == [str(expected)]
    if include_cloud:
        assert data['cloud_imaging_sources'][0]['current_url']==FIRST_URL
        assert data['cloud_imaging_sources'][0]['report_id']==str(report.pk)
        assert data['cloud_imaging_evidence'][0]['source_id']==str(cloud.pk)
    restored = read_structured_data(json.dumps(data))
    legacy = {**data, 'schema_version': '1.5'}
    if include_molecular:
        from apps.exports.errors import ExportInputError
        assert data['schema_version'] == '1.8'
        with pytest.raises(ExportInputError):
            read_structured_data(json.dumps(legacy))
        molecular_row = next(row for row in data['clinical_fields'] if row['id'] == str(metric.pk))
        assert molecular_row['content']['molecular_semantic_unit']['variants'][0]['identity'] == {
            key: value for key, value in identity.automatic_content['value'].items() if key != 'raw'}
    else:
        assert read_structured_data(json.dumps(legacy))['clinical_fields'] == data['clinical_fields']
    for key in ("documents", "facts", "labs", "sources", "clinical_reports", "clinical_fields", "clinical_field_sources",
                "self_records", *GLUCOSE_ARRAYS, *TREATMENT_ARRAYS, 'cloud_imaging_sources', 'cloud_imaging_evidence'):
        assert restored[key] == data[key]
    text = json.dumps(data, ensure_ascii=False)
    assert "UNSELECTED_DAILY" not in text and "UNSELECTED_GLUCOSE" not in text and "SYN-CLONE-A" not in text
    shared = create_share(patient, patient.account, scope).share
    assert {row['id'] for row in shared.snapshot['clinical_fields']} == expected_fields
    for key, expected in identities:
        assert [row["id"] for row in shared.snapshot[key]] == [str(expected)]
    if include_cloud:
        assert shared.snapshot['cloud_imaging_sources'][0]['id']==str(cloud.pk)
        assert FIRST_URL not in json.dumps(shared.snapshot)
    assert_snapshot_current(patient, snapshot)
    assert_snapshot_current(patient, shared.snapshot)
    with build_artifact(snapshot, {"format": "zip", "parts": ["json", "csv", "pdf"]}, InMemoryObjectStore()) as artifact:
        with zipfile.ZipFile(artifact.stream) as archive:
            assert {"csv/clinical_fields.csv", "csv/glucose_records.csv", "csv/self_records.csv", "csv/treatment_events.csv",
                    "csv/labs.csv", "visit-card.pdf"} <= set(archive.namelist())
            assert json.loads(archive.read("records.json")) == data


@pytest.mark.parametrize('include_cloud', [False, True])
def test_selected_lesion_and_ihc_keep_separate_contexts_in_roundtrip_and_share(django_user_model, include_cloud):
    from apps.lesions.readmodels import review_observations
    from apps.lesions.services import create_lesion
    from tests.lesions.factories import imaging_observation

    _, patient, document, _, fields = _graph(django_user_model, "pathology-lesion-mixed")
    _, imaging_document, report = imaging_observation(django_user_model, patient=patient, suv="3.2")
    observation = review_observations(patient, actor=patient.account)[0]
    create_lesion(patient, actor=patient.account, observation_id=observation["id"],
                  expected_revision=observation["revision_number"], expected_source=observation["source_token"],
                  name="Synthetic selected lesion", checked_original=True)
    lesion = patient.lesions.get()
    imaging_fields = [str(field.pk) for field in report.fields.filter(
        field_key__in=("lesion.site", "lesion.dimensions"))]
    scope = {**selection(document, fields["cps"]),
             "document_ids": [str(document.pk), str(imaging_document.pk)],
             "clinical_field_ids": [str(fields["cps"].pk), *imaging_fields],
             "lesion_ids": [str(lesion.pk)]}
    if include_cloud:
        from apps.cloud_imaging.readmodels import document_snapshot
        from apps.cloud_imaging.services import add_manual_source
        from tests.cloud_imaging.test_source_services import FIRST_URL, _decide
        original = document_snapshot(patient, actor=patient.account, document_id=imaging_document.pk)
        cloud = add_manual_source(patient, actor=patient.account, document_id=imaging_document.pk,
            page_id=imaging_document.pages.first().pk, report_id=report.pk, url=FIRST_URL,
            expected_source=original['input_token'], operation_id=uuid.uuid4())
        cloud = _decide(patient, cloud, 'CONFIRM')
        scope['cloud_source_ids'] = [str(cloud.pk)]
        scope['sections'] = ['imaging', 'cloud_imaging']
    snapshot = build_snapshot(patient, scope)
    data = read_structured_data(json_bytes(snapshot))
    assert data["schema_version"] == SCHEMA_VERSION
    if include_cloud:
        assert [row['id'] for row in data['cloud_imaging_sources']] == [str(cloud.pk)]
        assert data['cloud_imaging_sources'][0]['current_url'] == FIRST_URL
        assert data['cloud_imaging_evidence'][0]['source_id'] == str(cloud.pk)
    assert {row["id"] for row in data["clinical_fields"]} == set(scope["clinical_field_ids"])
    assert [row["id"] for row in data["lesions"]] == [str(lesion.pk)]
    assert len(data["lesion_observations"]) == len(data["lesion_measurements"]) == 1
    assert set(data["lesion_observations"][0]["field_ids"]) == set(imaging_fields)
    assert data["lesion_measurements"][0]["value"] == "12"
    assert data["lesion_measurements"][0]["context_field_ids"] == []
    assert "SYN-CLONE-A" not in json.dumps(data)
    shared = create_share(patient, patient.account, scope).share
    if include_cloud:
        assert shared.snapshot['cloud_imaging_sources'][0]['id'] == str(cloud.pk)
        assert FIRST_URL not in json.dumps(shared.snapshot)
    assert {row["id"] for row in shared.snapshot["clinical_fields"]} == set(scope["clinical_field_ids"])
    exported_fields = {row["id"]: row for row in data["clinical_fields"]}
    for row in shared.snapshot["clinical_fields"]:
        shared_content = deepcopy(row["content"])
        exported_content = deepcopy(exported_fields[row["id"]]["content"])
        if row["id"] == str(fields["cps"].pk):
            # Every output owns fresh aliases; sharing must not link these scopes across outputs.
            for role in ("specimen_scope", "assay_scope"):
                shared_token = shared_content["semantic_qualifiers"][role].pop("token")
                exported_token = exported_content["semantic_qualifiers"][role].pop("token")
                assert uuid.UUID(shared_token) != uuid.UUID(exported_token)
        assert shared_content == exported_content
        assert not row["source"].get("raw_text") and "url" not in row["source"]
    assert {row["fact_id"] for row in shared.snapshot["clinical_field_sources"]} == set(scope["clinical_field_ids"])
    for key in ("lesions", "lesion_observations", "lesion_measurements"):
        assert shared.snapshot[key] == snapshot[key] == data[key]
    assert_snapshot_current(patient, snapshot)
    assert_snapshot_current(patient, shared.snapshot)
    with build_artifact(snapshot, {"format": "zip", "parts": ["json", "csv", "pdf"]}, InMemoryObjectStore()) as artifact:
        with zipfile.ZipFile(artifact.stream) as archive:
            assert read_structured_data(archive.read("records.json")) == data
            assert {"csv/clinical_fields.csv", "csv/lesion_measurements.csv", "visit-card.pdf"} <= set(archive.namelist())
            if include_cloud:
                import csv
                from pypdf import PdfReader
                rows = list(csv.DictReader(io.StringIO(archive.read('csv/cloud_imaging_sources.csv').decode('utf-8-sig'))))
                assert rows[0]['current_url'] == FIRST_URL
                text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(archive.read('visit-card.pdf'))).pages)
                assert FIRST_URL in ''.join(text.split())


@pytest.mark.parametrize("key,value,expected", [
    ("specimen.dimensions", {"components": [{"value": "2.4", "unit": "cm", "axis": "LONG"},
        {"value": "1.2", "unit": None, "axis": "SHORT"}], "approximate": True,
        "measurement_role": "CURRENT", "measurement_object": "SPECIMEN", "raw": "UNSELECTED_OTHER_RESULT"},
        ["约", "2.4cm", "1.2（单位未注明）", "整体标本"]),
    ("specimen.nodes", {"groups": [{"label": "合成甲组", "sampled": None, "positive": "1", "raw": "UNSELECTED_OTHER_RESULT"},
        {"label": "合成乙组", "sampled": "4", "positive": "0", "raw": "UNSELECTED_OTHER_RESULT"}],
        "assertion": "SOURCE_TEXT_ONLY_NOT_DIAGNOSED", "raw": "UNSELECTED_OTHER_RESULT"},
        ["合成甲组：检出 未提供，阳性 1", "合成乙组：检出 4，阳性 0"]),
    ("specimen.histology", {"text": "不能排除合成描述", "assertion": "UNCERTAIN"},
        ["不能排除合成描述", "原文不确定"]),
])
def test_selected_non_ihc_pathology_preserves_object_missing_counts_and_assertion(django_user_model, key, value, expected):
    _, patient, document, report, fields = _graph(django_user_model, "pathology-portable-" + key)
    fact = add_field(patient, report, key, "specimen:a", value, {"SPECIMEN": fields["specimen"]})
    review(patient, fact)
    snapshot = build_snapshot(patient, selection(document, fact))
    data = json.loads(json_bytes(snapshot))
    assert len(data["clinical_fields"]) == 1
    field = data["clinical_fields"][0]
    for text in expected:
        assert text in field["content"]["text"]
    assert "UNSELECTED_OTHER_RESULT" not in json.dumps(data, ensure_ascii=False)
    restored = read_structured_data(json.dumps(data))
    assert restored["clinical_fields"] == data["clinical_fields"]
    if key == "specimen.nodes":
        assert field["content"]["value"]["groups"][0]["sampled"] is None
    elif key == "specimen.dimensions":
        assert field["content"]["value"]["components"][1]["unit"] is None


def test_explicit_original_selection_keeps_all_upload_bytes_and_fine_share_disallows_original_grant(django_user_model):
    from apps.documents.models import Document
    from apps.exports.errors import ExportInputError
    from pypdf import PdfWriter

    _, patient, document, _, fields = ihc_fixture(django_user_model, "pathology-explicit-original")
    stream, writer = io.BytesIO(), PdfWriter()
    writer.add_blank_page(width=400, height=600)
    writer.add_blank_page(width=400, height=600)
    writer.write(stream)
    payload = stream.getvalue()
    document.sha256, document.byte_size = hashlib.sha256(payload).hexdigest(), len(payload)
    Document.objects.filter(pk=document.pk).update(sha256=document.sha256, byte_size=document.byte_size, page_count=2)
    confirm_graph(patient, fields)
    scope = {**selection(document, fields["cps"]), "sections": ["imaging", "sources"]}
    snapshot = build_snapshot(patient, scope)
    assert snapshot["original_scope_warning"] is True
    store = InMemoryObjectStore()
    staged = store.put_staging(io.BytesIO(payload), expected_size=len(payload), expected_sha256=document.sha256)
    store.promote_immutable(staged, document.original_object_key)
    for parts in (["json"], ["json", "originals"]):
        with build_artifact(snapshot, {"format": "zip", "parts": parts}, store) as artifact:
            with zipfile.ZipFile(artifact.stream) as archive:
                originals = [path for path in archive.namelist() if path.startswith("originals/")]
                assert len(originals) == (1 if "originals" in parts else 0)
                if originals:
                    assert archive.read(originals[0]) == payload
                assert len(json.loads(archive.read("records.json"))["clinical_fields"]) == 1
    with pytest.raises(ExportInputError):
        create_share(patient, patient.account, scope, allow_original_download=True)
