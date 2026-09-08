"""Actual portable and recipient outputs retain the chosen source boundaries."""
from copy import deepcopy
from datetime import date
import json

import pytest

from apps.exports.content import build_snapshot
from apps.exports.formats import csv_tables, json_bytes
from apps.processing.models import DocumentMetadataCandidate
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_treatment_exports import selection
from tests.labs.test_trends import _observation
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_manual_events import create


pytestmark = pytest.mark.django_db


def _clone(instance, **changes):
    fields = {field.attname: deepcopy(getattr(instance, field.attname))
              for field in instance._meta.concrete_fields if not field.primary_key}
    return type(instance).objects.create(**{**fields, **changes})


def same_document_rows(patient):
    document, first = _observation(patient, date(2024, 3, 1), "2", code="LAB_NEUT_COUNT", page_count=4,
                                   raw_name="NEU#", standard_name="中性粒细胞计数")
    first.field_evidence.setdefault("observation_date", {})["page_number"] = 1
    first.save(update_fields=["field_evidence"])
    original_date = DocumentMetadataCandidate.objects.get(parsing_version=first.parsing_version, selected=True)
    rows = [first]
    for number, day, value in [(2, 2, "4"), (3, 3, "6"), (4, 13, "12")]:
        page = document.pages.get(page_number=number)
        evidence = _clone(first.evidence, document_page_id=page.pk, source_text=f"合成中性粒细胞 {value} 10^9/L")
        day_evidence = _clone(original_date.evidence, document_page_id=page.pk, source_text=f"采样日期：2024-03-{day:02}")
        _clone(original_date, evidence_id=day_evidence.pk, normalized_value=f"2024-03-{day:02}", raw_text=day_evidence.source_text)
        field_evidence = deepcopy(first.field_evidence)
        for proof in field_evidence.values():
            proof["page_number"] = number
        rows.append(_clone(first, document_page_id=page.pk, evidence_id=evidence.pk, observation_date=date(2024, 3, day),
                           raw_value=value, reading_order=number, field_evidence=field_evidence))
    return document, rows


def fine_scope(rows, key, *, current=-1):
    if key == "both":
        return {"observation_ids": [str(rows[1].pk), str(rows[current].pk)],
                "lab_ids": [str(rows[2].pk), str(rows[current].pk)]}
    return {key: [str(rows[current].pk)]}


def test_full_source_control_has_original_baseline_and_exact_previous_change(django_user_model):
    _, patient = _patient(django_user_model, "fine-control")
    document, rows = same_document_rows(patient)
    snapshot = build_snapshot(patient, selection([document], personal_change_ids=[str(rows[-1].pk)]))
    change = snapshot["personal_changes"][0]
    assert change["baseline_mean"] == "4" and change["baseline_percentage"] == "200"
    assert change["baseline_observation_ids"] == [str(row.pk) for row in rows[:-1]]
    assert change["daily_change"] == "0.6"


@pytest.mark.parametrize("key", ["observation_ids", "lab_ids", "both"])
def test_actual_json_and_csv_redact_unselected_same_document_dependencies(django_user_model, key):
    _, patient = _patient(django_user_model, "fine-change-" + key)
    document, rows = same_document_rows(patient)
    snapshot = build_snapshot(patient, selection([document], personal_change_ids=[str(rows[-1].pk)], **fine_scope(rows, key)))
    portable, tables = json.loads(json_bytes(snapshot)), csv_tables(snapshot)
    change = portable["personal_changes"][0]
    assert change["baseline_mean"] is None and change["baseline_percentage"] is None
    assert change["baseline_observation_ids"] == [] and change["previous_observation_id"] is None
    assert change["daily_change"] is None and change["elapsed_days"] is None
    assert change["baseline_reason"] == change["previous_reason"] == "missing_selected_source"
    derived = json.dumps({key: portable[key] for key in ("personal_changes", "derived_sources")})
    for row in rows[:-1]:
        assert str(row.pk) not in derived
        for name in ("personal_changes.csv", "derived_sources.csv"):
            assert str(row.pk) not in tables[name].decode("utf-8-sig")


@pytest.mark.parametrize("key", ["observation_ids", "lab_ids", "both"])
def test_actual_cycle_outputs_use_intersection_without_promoting_minimum_or_latest(django_user_model, key):
    _, patient = _patient(django_user_model, "fine-cycle-" + key)
    document, rows = same_document_rows(patient)
    current = cycle(patient, [create(patient, patient.account)])
    snapshot = build_snapshot(patient, selection([document], cycle_ids=[str(current.pk)], cycle_mode="full", **fine_scope(rows, key)))
    portable = json.loads(json_bytes(snapshot))
    assert {row["observation_id"] for row in portable["cycle_points"]} == {str(rows[-1].pk)}
    assert all(not {"OBSERVED_MIN", "LATEST"} & set(row["labels"]) for row in portable["cycle_points"])
    assert all(row["reason"] == "missing_selected_source" for row in portable["cycle_points"])
    tables = csv_tables(snapshot)
    for hidden in rows[:-1]:
        for name in ("cycle_points.csv", "cycle_key_nodes.csv", "derived_sources.csv", "cycle_links.csv"):
            assert str(hidden.pk) not in tables[name].decode("utf-8-sig")


@pytest.mark.parametrize("key", ["observation_ids", "lab_ids", "both"])
def test_actual_share_recipient_does_not_receive_hidden_personal_baselines(django_user_model, key):
    from apps.patients.sharing import create_share, exchange_share_token
    _, patient = _patient(django_user_model, "fine-share-" + key)
    reader, own = _patient(django_user_model, "fine-reader-" + key)
    document, rows = same_document_rows(patient)
    selected = selection([document], personal_change_ids=[str(rows[-1].pk)], **fine_scope(rows, key))
    created = create_share(patient, patient.account, selected)
    reader.get("/shared/open/")
    exchange_share_token(created.token, own.account, reader.session.session_key)
    response = reader.get(f"/shared/{created.share.pk}/")
    assert response.status_code == 200
    rendered = response.content.decode()
    change = response.context["snapshot"]["personal_changes"][0]
    assert change["baseline_mean"] is None and change["daily_change"] is None
    assert "200%" not in rendered and "0.6" not in rendered
    for row in rows[:-1]:
        assert str(row.pk) not in rendered
    assert reader.get(f"/labs/observations/{rows[-1].pk}/").status_code == 404


def test_fine_clinical_selection_does_not_implicitly_release_unselected_legacy_treatment_fact(django_user_model):
    from tests.treatments.factories import source_event
    _, patient = _patient(django_user_model, "fine-event-fact")
    event, _fact, document = source_event(patient)
    selected = selection([document], treatment_event_ids=[str(event.pk)], include_pending_cycles=True,
                         clinical_field_ids=[])
    portable = json.loads(json_bytes(build_snapshot(patient, selected)))
    assert portable["treatment_events"][0]["content"]["regimen_text"] is None
    assert portable["derived_sources"] == []


def test_clinical_fine_scope_requires_explicit_portable_observation_selection_for_derived_labs(django_user_model):
    _, patient = _patient(django_user_model, "fine-clinical-lab-default")
    document, rows = same_document_rows(patient)
    selected = selection([document], personal_change_ids=[str(rows[-1].pk)],
                         lab_ids=[str(row.pk) for row in rows], clinical_field_ids=[])
    portable = json.loads(json_bytes(build_snapshot(patient, selected)))
    assert portable["labs"] == []
    assert portable["personal_changes"][0]["current_value"] is None
    assert portable["personal_changes"][0]["baseline_mean"] is None
    assert portable["derived_sources"] == []


def test_disjoint_fine_selections_do_not_widen_to_either_list(django_user_model):
    _, patient = _patient(django_user_model, "fine-disjoint")
    document, rows = same_document_rows(patient)
    current = cycle(patient, [create(patient, patient.account)])
    portable = json.loads(json_bytes(build_snapshot(patient, selection([document],
        cycle_ids=[str(current.pk)], cycle_mode="full", observation_ids=[str(rows[1].pk)], lab_ids=[str(rows[2].pk)]))))
    assert portable["cycle_points"] == portable["cycle_key_nodes"] == []
    assert all(row["kind"] != "observation" for row in portable["derived_sources"])


def test_selected_middle_point_is_not_promoted_when_original_minimum_and_latest_are_omitted(django_user_model):
    _, patient = _patient(django_user_model, "fine-middle")
    document, rows = same_document_rows(patient)
    current = cycle(patient, [create(patient, patient.account)])
    snapshot = build_snapshot(patient, selection([document], cycle_ids=[str(current.pk)], cycle_mode="full",
                                               observation_ids=[str(rows[1].pk)]))
    point, = snapshot["cycle_points"]
    assert point["value"] == "4" and point["labels"] == []
    absent = {node["kind"] for node in snapshot["cycle_key_nodes"] if node["reason"] == "missing_selected_source"}
    assert {"OBSERVED_MIN", "LATEST"} <= absent


def test_intersection_validation_does_not_hide_foreign_lab_identifiers(django_user_model):
    from apps.exports.errors import ExportInputError
    _, patient = _patient(django_user_model, "fine-invalid-target")
    _, foreign = _patient(django_user_model, "fine-foreign-target")
    document, rows = same_document_rows(patient)
    _, other = _observation(foreign, date(2024, 3, 1), "99")
    with pytest.raises(ExportInputError):
        build_snapshot(patient, selection([document], personal_change_ids=[str(rows[-1].pk)],
            observation_ids=[str(rows[-1].pk)], lab_ids=[str(rows[-1].pk), str(other.pk)]))
