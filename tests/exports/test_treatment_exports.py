from datetime import date
from copy import deepcopy
import json
import uuid

import pytest
from django.core.exceptions import PermissionDenied

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_manual_events import create

pytestmark = pytest.mark.django_db


def selection(documents=(), **kwargs):
    return {"mode": "documents", "document_ids": [str(row.pk) for row in documents], "nickname": "合成患者",
            "sections": ["treatment", "labs"], "details": True, **kwargs}


def test_explicit_user_treatment_is_a_real_standalone_source_without_fabricating_document(django_user_model):
    _, patient = _patient(django_user_model, "treatment-portable-user")
    event = create(patient, patient.account)
    snapshot = build_snapshot(patient, selection(treatment_event_ids=[str(event.pk)]))
    assert snapshot["schema_version"] == "1.3"
    assert snapshot["documents"] == [] and snapshot["self_records"] == []
    assert [row["id"] for row in snapshot["treatment_events"]] == [str(event.pk)]
    assert snapshot["treatment_events"][0]["content"]["recorded_as"] == "USER"
    assert_snapshot_current(patient, snapshot)


def test_legacy_selection_does_not_implicitly_export_treatment_or_personal_changes(django_user_model):
    _, patient = _patient(django_user_model, "treatment-portable-default")
    document, _ = _observation(patient, date(2024, 3, 1), "2")
    create(patient, patient.account)
    snapshot = build_snapshot(patient, selection([document]))
    for key in ("treatment_events", "treatment_regimens", "treatment_cycles", "cycle_links", "cycle_points", "cycle_key_nodes", "personal_changes", "derived_sources"):
        assert snapshot[key] == []


def test_personal_changes_keep_full_precision_and_exact_previous_three_sources(django_user_model):
    _, patient = _patient(django_user_model, "treatment-portable-change")
    sources = [_observation(patient, date(2024, 3, day), value) for day, value in [(1, "2"), (2, "4"), (3, "6"), (13, "12")]]
    snapshot = build_snapshot(patient, selection([row[0] for row in sources], personal_change_ids=[str(sources[-1][1].pk)]))
    change = snapshot["personal_changes"][0]
    assert change["daily_change"] == "0.6" and change["baseline_mean"] == "4"
    assert change["baseline_observation_ids"] == [str(row[1].pk) for row in sources[:3]]
    assert {row["source_id"] for row in snapshot["derived_sources"] if row["kind"] == "observation"} == {str(row[1].pk) for row in sources}


def test_missing_baseline_documents_redacts_original_dependencies_without_recomputing_a_new_baseline(django_user_model):
    _, patient = _patient(django_user_model, "treatment-portable-partial")
    sources = [_observation(patient, date(2024, 3, day), value) for day, value in [(1, "2"), (2, "4"), (3, "6"), (13, "12")]]
    snapshot = build_snapshot(patient, selection([sources[-1][0]], personal_change_ids=[str(sources[-1][1].pk)]))
    change = snapshot["personal_changes"][0]
    assert change["baseline_mean"] is None and change["baseline_percentage"] is None
    assert change["baseline_observation_ids"] == [] and change["previous_observation_id"] is None
    assert change["daily_change"] is None and change["elapsed_days"] is None
    assert change["baseline_reason"] == change["previous_reason"] == "missing_selected_source"
    derived = json.dumps({key: snapshot[key] for key in ("personal_changes", "derived_sources")})
    for document, row in sources[:3]:
        assert str(document.pk) not in derived and str(row.pk) not in derived


def test_cycle_output_is_closed_over_user_anchor_and_selected_labs_with_actual_relative_days(django_user_model):
    _, patient = _patient(django_user_model, "treatment-portable-cycle")
    event = create(patient, patient.account)
    current = cycle(patient, [event])
    sources = [_observation(patient, date(2024, 2, 28), "4", code="LAB_NEUT_COUNT"),
               _observation(patient, date(2024, 3, 2), "1", code="LAB_NEUT_COUNT")]
    snapshot = build_snapshot(patient, selection([item[0] for item in sources], cycle_ids=[str(current.pk)], cycle_mode="full"))
    assert [row["id"] for row in snapshot["treatment_cycles"]] == [str(current.pk)]
    assert {row["relative_day"] for row in snapshot["cycle_points"]} == {-1, 2}
    assert [row["id"] for row in snapshot["treatment_events"]] == [str(event.pk)]


def test_missing_real_anchor_document_does_not_leak_derived_anchor_or_points(django_user_model):
    from tests.treatments.factories import source_event
    _, patient = _patient(django_user_model, "treatment-portable-anchor-scope")
    event, _, anchor_document = source_event(patient)
    current = cycle(patient, [event])
    lab_document, _ = _observation(patient, date(2024, 3, 2), "1", code="LAB_NEUT_COUNT")
    snapshot = build_snapshot(patient, selection([lab_document], cycle_ids=[str(current.pk)], cycle_mode="full"))
    row = snapshot["treatment_cycles"][0]
    assert row["content"]["anchor"] is None and row["reason"] == "missing_selected_source"
    assert snapshot["cycle_points"] == []
    assert str(anchor_document.pk) not in json.dumps(snapshot["derived_sources"])


@pytest.mark.parametrize("key", ["treatment_event_ids", "regimen_ids", "cycle_ids", "personal_change_ids"])
def test_unknown_or_foreign_derived_ids_are_rejected_not_silently_omitted(django_user_model, key):
    _, patient = _patient(django_user_model, "treatment-portable-foreign-" + key)
    document, _ = _observation(patient, date(2024, 3, 1), "2")
    with pytest.raises(PermissionDenied):
        build_snapshot(patient, selection([document], **{key: [str(uuid.uuid4())]}))


def test_revised_user_treatment_invalidates_existing_snapshot(django_user_model):
    from apps.treatments.services import revise_event
    _, patient = _patient(django_user_model, "treatment-portable-revision")
    event = create(patient, patient.account)
    snapshot = build_snapshot(patient, selection(treatment_event_ids=[str(event.pk)]))
    revise_event(patient, event.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                 operation_id=uuid.uuid4(), checked_original=True, changes={"note": "新的明确补记"})
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)


def test_portable_minor_version_retains_older_clinical_and_self_record_tables(django_user_model):
    from apps.exports.formats import csv_tables, json_bytes, read_structured_data
    _, patient = _patient(django_user_model, "treatment-portable-formats")
    event = create(patient, patient.account, title="=合成安全文本")
    snapshot = build_snapshot(patient, selection(treatment_event_ids=[str(event.pk)]))
    value = read_structured_data(json_bytes(snapshot))
    assert value["schema_version"] == "1.3"
    assert value["treatment_events"][0]["content"]["title"] == "=合成安全文本"
    tables = csv_tables(snapshot)
    assert {"clinical_reports.csv", "clinical_fields.csv", "clinical_field_sources.csv", "self_records.csv", "treatment_events.csv", "cycle_points.csv", "personal_changes.csv"} <= tables.keys()
    for version in ["1.0", "1.1", "1.2"]:
        old = deepcopy(value)
        old["schema_version"] = version
        old.pop("treatment_events")
        assert read_structured_data(json.dumps(old))["treatment_events"] == []


def test_actual_pdf_and_screen_sections_include_selected_user_treatment(django_user_model):
    from io import BytesIO
    from pypdf import PdfReader
    from apps.exports.pdf import card_sections, render_pdf
    _, patient = _patient(django_user_model, "treatment-portable-pdf")
    event = create(patient, patient.account, title="本次选择的治疗记录")
    snapshot = build_snapshot(patient, selection(treatment_event_ids=[str(event.pk)]))
    assert "本次选择的治疗记录" in json.dumps(card_sections(snapshot), ensure_ascii=False)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(render_pdf(snapshot))).pages)
    assert "本次选择的治疗记录" in text and "本人补记" in text


def test_export_http_form_keeps_derived_selection_in_the_actual_snapshot(django_user_model):
    from tests.patients.test_family_access import family
    from apps.exports.models import ExportJob
    _, patient, client, actor, _ = family(django_user_model, "treatment-form-export")
    event = create(patient, actor)
    response = client.post("/visit/", {**selection(treatment_event_ids=[str(event.pk)]), "patient_id": str(patient.pk), "action": "preview"})
    assert response.status_code == 302
    job = ExportJob.objects.get(patient=patient)
    assert [row["id"] for row in job.snapshot["treatment_events"]] == [str(event.pk)]


def test_share_http_form_keeps_derived_selection_without_requiring_any_document(django_user_model):
    from tests.patients.test_family_access import family
    from apps.patients.models import PatientShare
    owner, patient, _, actor, _ = family(django_user_model, "treatment-form-share")
    event = create(patient, actor)
    response = owner.post(f"/patients/{patient.pk}/shares/", {**selection(treatment_event_ids=[str(event.pk)]), "expires_in_hours": 24})
    assert response.status_code == 201
    share = PatientShare.objects.get(patient=patient)
    assert [row["id"] for row in share.snapshot["treatment_events"]] == [str(event.pk)]


def test_filtered_cycle_points_do_not_promote_a_new_minimum_or_latest_label(django_user_model):
    _, patient = _patient(django_user_model, "treatment-filtered-minimum")
    current = cycle(patient, [create(patient, patient.account)])
    sources = [_observation(patient, date(2024, 3, day), value, code="LAB_NEUT_COUNT") for day, value in [(1, "4"), (2, "1"), (3, "3")]]
    snapshot = build_snapshot(patient, selection([sources[0][0], sources[2][0]], cycle_ids=[str(current.pk)], cycle_mode="full"))
    assert len(snapshot["cycle_points"]) == 2
    assert all("OBSERVED_MIN" not in row["labels"] and "LATEST" not in row["labels"] for row in snapshot["cycle_points"])
    assert all(row["reason"] == "missing_selected_source" for row in snapshot["cycle_points"])
    assert str(sources[1][1].pk) not in json.dumps(snapshot["cycle_key_nodes"])


def test_pdf_pending_content_is_only_on_an_explicit_candidate_appendix(django_user_model):
    from io import BytesIO
    from pypdf import PdfReader
    from apps.exports.pdf import render_pdf
    from apps.exports.errors import PdfUnavailable
    from apps.treatments.services import revise_event
    _, patient = _patient(django_user_model, "treatment-pdf-candidate")
    event = create(patient, patient.account, title="候选专用合成记录")
    revise_event(patient, event.pk, actor=patient.account, action="REVOKE", expected_revision=1, operation_id=uuid.uuid4())
    with pytest.raises(ExportInputError):
        build_snapshot(patient, selection(treatment_event_ids=[str(event.pk)]))
    snapshot = build_snapshot(patient, selection(treatment_event_ids=[str(event.pk)], include_pending_cycles=True))
    pages = PdfReader(BytesIO(render_pdf(snapshot))).pages
    assert len(pages) >= 2
    assert "候选专用合成记录" not in pages[0].extract_text()
    assert "候选专用合成记录" in "\n".join(page.extract_text() for page in pages[1:])
    snapshot["card"]["details"] = False
    with pytest.raises(PdfUnavailable):
        render_pdf(snapshot)


def test_key_node_missing_reasons_are_portable_without_invented_observation_ids(django_user_model):
    _, patient = _patient(django_user_model, "treatment-portable-missing-node")
    current = cycle(patient, [create(patient, patient.account)])
    document, _ = _observation(patient, date(2024, 3, 2), "2", code="LAB_NEUT_COUNT")
    snapshot = build_snapshot(patient, selection([document], cycle_ids=[str(current.pk)]))
    absent = [row for row in snapshot["cycle_key_nodes"] if row.get("reason") == "no_comparable_prior_day"]
    assert len(absent) == 1 and absent[0]["point_id"] is None and absent[0]["observation_id"] is None
