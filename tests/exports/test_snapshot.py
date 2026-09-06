from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.labs.revisions import revise_observation
from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.factories import parsed_facts
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_visit_preparation_entry_is_reachable(django_user_model):
    client, _patient_record = _patient(django_user_model, "visit-entry")
    assert client.get("/visit/").status_code == 200


def test_date_selection_is_inclusive_and_uncertain_dates_require_explicit_choice(django_user_model):
    from apps.exports.selection import select_documents

    _, patient = _patient(django_user_model, "visit-dates")
    first, _ = _observation(patient, date(2026, 8, 1), "4")
    last, _ = _observation(patient, date(2026, 8, 31), "5")
    outside, _ = _observation(patient, date(2026, 9, 1), "6")
    partial, _ = parsed_facts(patient, ["诊断：未明确。"])
    unknown, _ = _document(patient)
    selected = select_documents(patient, {"mode": "dates", "start": "2026-08-01", "end": "2026-08-31"})
    assert {item["id"] for item in selected["documents"]} == {str(first.pk), str(last.pk), str(partial.pk)}
    assert {item["id"] for item in selected["uncertain"]} == {str(unknown.pk)}
    assert any(item["id"] == str(outside.pk) for item in selected["excluded"])
    narrowed = select_documents(patient, {"mode": "dates", "start": "2026-08-15", "end": "2026-08-31"})
    assert {item["id"] for item in narrowed["uncertain"]} == {str(partial.pk), str(unknown.pk)}
    chosen = select_documents(patient, {
        "mode": "dates", "start": "2026-08-15", "end": "2026-08-31", "unknown_ids": [str(partial.pk)],
    })
    assert {item["id"] for item in chosen["documents"]} == {str(last.pk), str(partial.pk)}


def test_snapshot_admits_confirmed_facts_and_preserves_raw_effective_labs_and_same_day_duplicates(django_user_model):
    from apps.exports.content import build_snapshot

    _, patient = _patient(django_user_model, "visit-content")
    _, version = parsed_facts(patient, ["诊断：考虑炎症。", "分期：未明确。"])
    diagnosis = Fact.objects.get(parsing_version=version, category="DIAGNOSIS")
    revise_fact(patient, diagnosis.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    _, first = _observation(patient, date(2026, 8, 20), "<4.20", result_type="COMPARATOR")
    _, second = _observation(patient, date(2026, 8, 20), "5.0")
    _, suspect = _observation(patient, date(2026, 8, 21), "999")
    revise_observation(patient.account, second.pk, action="CORRECT", changes={"raw_value": "6.0"}, expected_revision=0)
    revise_observation(patient.account, suspect.pk, action="REPORT_ERROR", changes={}, expected_revision=0)
    snapshot = build_snapshot(patient, {"mode": "all"})
    assert [item["id"] for item in snapshot["facts"]] == [str(diagnosis.pk)]
    assert len(snapshot["labs"]) == 3
    by_id = {item["id"]: item for item in snapshot["labs"]}
    assert by_id[str(first.pk)]["comparator"] == "<"
    assert by_id[str(first.pk)]["value"] == "<4.20"
    assert by_id[str(second.pk)]["raw_value"] == "5.0" and by_id[str(second.pk)]["value"] == "6.0"
    assert by_id[str(second.pk)]["revision_id"] is not None
    assert by_id[str(suspect.pk)]["quality_issues"]
    assert str(suspect.pk) not in snapshot["card"]["lab_ids"]
    assert {str(first.pk), str(second.pk)} <= set(snapshot["card"]["lab_ids"])
    assert [item["key"] for item in snapshot["card"]["sections"]] == ["patient", "diagnosis", "treatment", "labs", "imaging", "sources"]
    assert snapshot["excluded_facts"][0]["reason"] == "尚未核对"


def test_snapshot_changes_are_detected_after_revision_revoke_reparse_and_trash_restore(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document
    from apps.exports.content import build_snapshot, assert_snapshot_current
    from apps.exports.errors import SnapshotChanged

    _, patient = _patient(django_user_model, "visit-current")
    document, version = parsed_facts(patient, ["诊断：考虑炎症。"])
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, action="CONFIRM", expected_revision=0, checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all"})
    assert_snapshot_current(patient, snapshot)
    revise_fact(patient, fact.pk, action="REVOKE", expected_revision=1)
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)
    current = build_snapshot(patient, {"mode": "all"})
    move_to_trash(patient, document.pk)
    restore_document(patient, document.pk)
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, current)
    current = build_snapshot(patient, {"mode": "all"})
    parsed_facts(patient, ["诊断：考虑炎症。"], document=document, previous=version)
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, current)


def test_snapshot_freezes_selected_documents_and_rejects_foreign_or_empty_selection(django_user_model):
    from apps.exports.content import build_snapshot, assert_snapshot_current
    from apps.exports.errors import ExportInputError

    _, patient = _patient(django_user_model, "visit-scope")
    _, other = _patient(django_user_model, "visit-foreign")
    own, _ = _document(patient)
    foreign, _ = _document(other)
    snapshot = build_snapshot(patient, {"mode": "all"})
    _document(patient)
    assert_snapshot_current(patient, snapshot)
    assert len(snapshot["documents"]) == 1
    with pytest.raises(PermissionDenied):
        build_snapshot(patient, {"mode": "documents", "document_ids": [str(foreign.pk)]})
    with pytest.raises(ExportInputError):
        build_snapshot(patient, {"mode": "documents", "document_ids": []})
    with pytest.raises(ExportInputError):
        build_snapshot(patient, {"mode": "dates", "start": "2026-09-01", "end": "2026-08-01"})
    assert snapshot["documents"][0]["id"] == str(own.pk)


def test_comparable_trend_is_only_built_from_selected_trustworthy_results(django_user_model):
    from apps.exports.content import build_snapshot

    _, patient = _patient(django_user_model, "visit-trend")
    first_doc, _ = _observation(patient, date(2026, 7, 1), "4.0")
    last_doc, _ = _observation(patient, date(2026, 8, 20), "5.0")
    snapshot = build_snapshot(patient, {"mode": "all"})
    assert len(snapshot["card"]["trends"]) == 1
    assert [point["value"] for point in snapshot["card"]["trends"][0]["points"]] == ["4.0", "5.0"]
    selected = build_snapshot(patient, {"mode": "documents", "document_ids": [str(last_doc.pk)]})
    assert selected["card"]["trends"] == []


def test_selected_labs_keep_existing_history_quality_context_and_incomplete_dates_are_not_trend_days(django_user_model, monkeypatch):
    from apps.exports.content import build_snapshot

    _, patient = _patient(django_user_model, "visit-quality-history")
    _, earlier = _observation(patient, date(2026, 7, 1), "5.2")
    document, later = _observation(patient, date(2026, 8, 20), "52")
    for row in (earlier, later):
        row.specimen = "BLOOD"
        row.save(update_fields=["specimen"])
    rule = dict(id="synthetic-export-history", version="fixture-1", kind="history_ratio", code="LAB_WBC",
                unit="10^9/L", minimum_ratio="10", specimen="BLOOD", method="合成方法A",
                reviewed_by="fixture", rationale="合成边界测试")
    monkeypatch.setattr("apps.labs.comparison.rules_for_version", lambda _: [rule])
    snapshot = build_snapshot(patient, {"mode": "documents", "document_ids": [str(document.pk)]})
    assert "magnitude_suspect" in {item["code"] for item in snapshot["labs"][0]["quality_issues"]}
    assert snapshot["card"]["lab_ids"] == []
    monkeypatch.setattr("apps.labs.comparison.rules_for_version", lambda _: [])
    _, other = _patient(django_user_model, "visit-months")
    _observation(other, date(2026, 7, 1), "4", precision="MONTH")
    _observation(other, date(2026, 8, 1), "5", precision="MONTH")
    snapshot = build_snapshot(other, {"mode": "all"})
    assert len(snapshot["card"]["lab_ids"]) == 2
    assert snapshot["card"]["trends"] == []
