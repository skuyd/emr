"""Portable 1.2 preserves each selected clinical value and daily record fence."""
from apps.facts.laterality import review_parent_arguments

import json
from uuid import uuid4

import pytest

from apps.exports.errors import ExportUnavailable
from apps.exports.formats import json_bytes, read_structured_data
from apps.exports.models import ExportJob
from apps.exports.services import get_preview
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientShare
from apps.patients.sharing import validate_managed_share
from apps.self_records.services import create_record, revise_record
from tests.facts.test_imaging_quantitative import fields, imaging
from tests.self_records.test_payloads import payload


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("key", ["lesion.suvmax", "lesion.maximum_scope", "comparison.statement", "comparison.reference_date"])
@pytest.mark.parametrize("change", ["record", "field"])
def test_actual_mixed_selection_keeps_typed_contract_and_both_revision_fences(django_user_model, key, change):
    client, patient, document, _, _ = imaging(django_user_model,
        "双肺结节，较大者位于左肺上叶，约12mm，SUVmax≤4.20。",
        impression="对比前片（2025年06月）：左肺结节同前。UNSELECTED_REPORT_CANARY。",
        name="imaging-daily-" + key.replace(".", "-") + change)
    selected = fields(document, key)[0]
    revise_fact(patient, selected.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(selected)["current_source_token"], checked_original=True, **review_parent_arguments(selected))
    record = create_record(patient, patient.account, payload(notes="selected daily entry"), creation_key=uuid4()).record
    create_record(patient, patient.account, payload(notes="UNSELECTED_DAILY_CANARY"), creation_key=uuid4())
    scope = {"mode": "documents", "document_ids": [str(document.pk)], "clinical_field_ids": [str(selected.pk)],
             "self_record_ids": [str(record.pk)], "sections": ["patient", "imaging", "self_records"]}
    assert client.get("/visit/").status_code == 200
    response = client.post("/visit/", {**scope, "patient_id": str(patient.pk), "nickname": "Synthetic patient",
                                      "custom_clinical_fields": "on", "details": "on", "action": "preview"})
    assert response.status_code == 302, (response.context["form"].errors, response.context["error"])
    job = ExportJob.objects.get(patient=patient)
    encoded = json_bytes(job.snapshot)
    data = read_structured_data(encoded)
    assert data["schema_version"] == "1.5"
    assert [f["id"] for f in data["clinical_fields"]] == [str(selected.pk)]
    assert data["clinical_fields"][0]["schema_version"] == "1.1"
    assert data["clinical_fields"][0]["content"]["value"] == selected.automatic_content["value"]
    assert [r["id"] for r in data["self_records"]] == [str(record.pk)]
    assert data["facts"] == data["labs"] == []
    assert "UNSELECTED_DAILY_CANARY" not in encoded.decode() and "UNSELECTED_REPORT_CANARY" not in encoded.decode()
    response = client.post(f"/patients/{patient.pk}/shares/", {**scope, "expires_in_hours": 24})
    assert response.status_code == 201
    share = PatientShare.objects.get(patient=patient)
    assert [r["id"] for r in share.snapshot["self_records"]] == [str(record.pk)]
    assert [f["id"] for f in share.snapshot["clinical_fields"]] == [str(selected.pk)]
    assert "UNSELECTED_DAILY_CANARY" not in json.dumps(share.snapshot)
    assert "UNSELECTED_REPORT_CANARY" not in json.dumps(share.snapshot)
    assert not share.allow_original_download
    if change == "record":
        revise_record(patient, patient.account, record.pk, action="CORRECT", expected_revision=0, changes=payload(value="61"))
    else:
        selected.refresh_from_db()
        revise_fact(patient, selected.pk, actor=patient.account, action="REVOKE", expected_revision=1,
                    expected_source=effective_fact(selected)["current_source_token"], **review_parent_arguments(selected))
    with pytest.raises(ExportUnavailable):
        get_preview(patient, client.session.session_key, job.pk, actor=patient.account)
    validate_managed_share(patient, patient.account, share.pk)
    job.refresh_from_db()
    share.refresh_from_db()
    assert job.snapshot == {} and job.status == "INVALIDATED"
    assert share.snapshot == {} and share.invalidated_at is not None
