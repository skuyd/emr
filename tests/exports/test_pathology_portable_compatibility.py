"""New context semantics coexist with every already merged selected domain."""
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
def test_ihc_selection_preserves_actual_glucose_daily_lab_treatment_tables_and_roundtrip(django_user_model,include_cloud):
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
    snapshot = build_snapshot(patient, scope)
    data = json.loads(json_bytes(snapshot))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["cancer_candidates"] == []
    assert data["indicator_ordering"] == []
    identities = (("clinical_fields", fields["cps"].pk), ("labs", lab.pk), ("self_records", daily.pk),
                  ("glucose_records", glucose.pk), ("treatment_events", event.pk))
    for key, expected in identities:
        assert [row["id"] for row in data[key]] == [str(expected)]
    if include_cloud:
        assert data['cloud_imaging_sources'][0]['current_url']==FIRST_URL
        assert data['cloud_imaging_sources'][0]['report_id']==str(report.pk)
        assert data['cloud_imaging_evidence'][0]['source_id']==str(cloud.pk)
    restored = read_structured_data(json.dumps(data))
    for key in ("documents", "facts", "labs", "sources", "clinical_reports", "clinical_fields", "clinical_field_sources",
                "self_records", *GLUCOSE_ARRAYS, *TREATMENT_ARRAYS, 'cloud_imaging_sources', 'cloud_imaging_evidence'):
        assert restored[key] == data[key]
    text = json.dumps(data, ensure_ascii=False)
    assert "UNSELECTED_DAILY" not in text and "UNSELECTED_GLUCOSE" not in text and "SYN-CLONE-A" not in text
    shared = create_share(patient, patient.account, scope).share
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
