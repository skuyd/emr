"""Selected clinical meaning and source boundaries through real export services."""
from copy import deepcopy
import io
import json
import zipfile

import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
from apps.exports.pdf import card_sections
from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from apps.patients.sharing_content import normalize_scope, project_snapshot
from tests.documents.fakes import InMemoryObjectStore
from tests.facts.pathology_factories import add_field, confirm_graph, ihc_fixture, review, score_value


pytestmark = pytest.mark.django_db
POLICY = "IHC_SCORE_SEMANTIC_UNIT_V1"


def selection(document, *fields):
    return {"mode": "documents", "document_ids": [str(document.pk)],
            "clinical_field_ids": [str(field.pk) for field in fields], "sections": ["imaging"]}


def _public_text(snapshot):
    text = json_bytes(snapshot).decode("utf-8")
    text += "\n".join(table.decode("utf-8-sig") for table in csv_tables(snapshot).values())
    text += json.dumps(card_sections(snapshot), ensure_ascii=False)
    artifact = build_artifact(snapshot, {"format": "zip", "parts": ["json", "csv"]}, InMemoryObjectStore())
    with zipfile.ZipFile(io.BytesIO(artifact.payload)) as bundle:
        for name in bundle.namelist():
            if name.endswith((".json", ".csv")):
                text += bundle.read(name).decode("utf-8-sig")
    return text


def _graph(django_user_model, name):
    client, patient, document, report, fields = ihc_fixture(django_user_model, name)
    confirm_graph(patient, fields)
    return client, patient, document, report, fields


def test_score_only_json_csv_zip_and_card_retain_semantic_unit_without_whole_context(django_user_model):
    _, patient, document, report, fields = _graph(django_user_model, "path-export-score")
    value = score_value("CPS", "21", None)
    value["raw"] = "CPS 21；UNSELECTED_RAW_CLAUSE；SYN-CLONE-A"
    review(patient, fields["cps"], "CORRECT", {"value": value, "raw_value": value["raw"]})
    snapshot = build_snapshot(patient, selection(document, fields["cps"]))
    field = snapshot["clinical_fields"][0]
    qualifiers = field["content"].get("semantic_qualifiers", {})
    assert qualifiers.get("marker") == {"code": "PD_L1", "label": "PD-L1"}
    assert qualifiers["policy"] == POLICY and qualifiers["binding_state"] == "RESOLVED"
    assert qualifiers["assay_conditions"] == "NOT_INCLUDED_NOT_COMPARABLE"
    assert field["content"]["value"]["unit"] is None
    assert field["content"]["value"]["unit_state"] == "NOT_PRINTED"
    assert field["content"]["value"]["scale_kind"] == "SCORE"
    assert qualifiers["qualitative_result"] == "AS_REPORTED_NO_POSITIVITY_INFERRED"
    text = _public_text(snapshot)
    for excluded in ("UNSELECTED_RAW_CLAUSE", "synthetic-report.pdf", "合成病理与IHC报告",
                     "SYN-CLONE-A", "标本甲", "检测甲", "entity_context", "dependency_heads", "context_snapshot"):
        assert excluded not in text
    assert "PD-L1 CPS 21" in text and "单位未印刷" in text
    assert "选定标本 1" in text and "选定检测 1" in text and "不可据此判断可比" in text
    assert {field["id"] for field in snapshot["clinical_fields"]} == {str(fields["cps"].pk)}
    assert all(row["raw_text"] == "" and row["start_offset"] is None and row["end_offset"] is None
               for row in snapshot["clinical_field_sources"])
    assert snapshot["clinical_reports"][0]["id"] == str(report.pk)
    assert snapshot["clinical_reports"][0]["spans"] == []
    for key in ("specimen", "assay", "marker", "clone", "tps"):
        assert str(fields[key].pk) not in text
    assert_snapshot_current(patient, snapshot)


def test_scope_aliases_group_only_current_selection_and_are_not_stable_cross_export_ids(django_user_model):
    _, patient, document, _, fields = _graph(django_user_model, "path-export-alias")
    first = build_snapshot(patient, selection(document, fields["tps"], fields["cps"]))
    bundles = [row["content"].get("semantic_qualifiers", {}) for row in first["clinical_fields"]]
    assert all("specimen_scope" in bundle and "assay_scope" in bundle for bundle in bundles)
    assert bundles[0]["specimen_scope"] == bundles[1]["specimen_scope"]
    assert bundles[0]["assay_scope"] == bundles[1]["assay_scope"]
    second = build_snapshot(patient, selection(document, fields["cps"]))
    second_bundle = second["clinical_fields"][0]["content"]["semantic_qualifiers"]
    assert second_bundle["specimen_scope"]["token"] != bundles[0]["specimen_scope"]["token"]
    assert second_bundle["assay_scope"]["token"] != bundles[0]["assay_scope"]["token"]


def test_same_marker_different_specimen_and_assay_stay_distinct_without_names(django_user_model):
    _, patient, document, report, fields = _graph(django_user_model, "path-export-two")
    raw = "标本乙；检测乙；PD-L1；TPS 2%"
    specimen = add_field(patient, report, "specimen.identity", "specimen:b", {"label": "标本乙", "raw": "标本乙"}, {}, raw=raw)
    assay = add_field(patient, report, "assay.identity", "assay:b", {"label": "检测乙", "raw": "检测乙"}, {"SPECIMEN": specimen}, raw=raw)
    marker = add_field(patient, report, "ihc.marker", "ihc:b", {"code": "PD_L1", "label": "PD-L1", "raw": "PD-L1"},
                       {"SPECIMEN": specimen, "ASSAY": assay}, raw=raw)
    second = add_field(patient, report, "ihc.score", "ihc:b", score_value(number="2"),
                       {"SPECIMEN": specimen, "ASSAY": assay, "MARKER": marker}, raw=raw)
    for item in (specimen, assay, marker, second):
        review(patient, item)
    snapshot = build_snapshot(patient, selection(document, fields["tps"], second))
    bundles = [field["content"].get("semantic_qualifiers", {}) for field in snapshot["clinical_fields"]]
    assert all("specimen_scope" in bundle for bundle in bundles)
    assert len({bundle["specimen_scope"]["token"] for bundle in bundles}) == 2
    assert len({bundle["assay_scope"]["token"] for bundle in bundles}) == 2
    text = _public_text(snapshot)
    assert "标本乙" not in text and "检测乙" not in text


def test_explicit_selected_condition_uses_same_assay_alias_without_releasing_other_fields(django_user_model):
    _, patient, document, _, fields = _graph(django_user_model, "path-export-condition")
    snapshot = build_snapshot(patient, selection(document, fields["cps"], fields["clone"]))
    rows = {row["id"]: row for row in snapshot["clinical_fields"]}
    score = rows[str(fields["cps"].pk)]["content"].get("semantic_qualifiers", {})
    condition = rows[str(fields["clone"].pk)]["content"].get("semantic_qualifiers", {})
    assert score.get("assay_scope") and score["assay_scope"] == condition.get("assay_scope")
    assert score["assay_conditions"] == "SEE_SELECTED_FIELDS_NOT_COMPARABLE"
    text = _public_text(snapshot)
    assert "SYN-CLONE-A" in text and "TPS 13" not in text and "检测甲" not in text


def test_whole_snapshot_can_be_finely_shared_without_losing_qualifiers_or_leaking_whole_context(django_user_model):
    _, patient, document, _, fields = _graph(django_user_model, "path-share-projection")
    snapshot = build_snapshot(patient, {"mode": "documents", "document_ids": [str(document.pk)]})
    scope = normalize_scope(selection(document, fields["cps"]))
    shared = project_snapshot(snapshot, scope)
    assert len(shared["clinical_fields"]) == 1
    qualifiers = shared["clinical_fields"][0]["content"].get("semantic_qualifiers", {})
    assert qualifiers.get("marker") == {"code": "PD_L1", "label": "PD-L1"}
    assert qualifiers["assay_conditions"] == "NOT_INCLUDED_NOT_COMPARABLE"
    text = json.dumps(shared, ensure_ascii=False)
    for excluded in ("SYN-CLONE-A", "标本甲", "检测甲", "synthetic-report.pdf", "合成病理与IHC报告",
                     "entity_context", "context_snapshot", "dependency_heads"):
        assert excluded not in text


def test_current_unknown_assay_never_exports_as_legacy_bare_scalar(django_user_model):
    _, patient, document, report, _ = ihc_fixture(django_user_model, "path-export-unknown")
    field = add_field(patient, report, "ihc.score", "ihc:unknown", score_value(),
                      {"SPECIMEN": None, "ASSAY": None, "MARKER": None})
    review(patient, field)
    with pytest.raises(ExportInputError):
        build_snapshot(patient, selection(document, field))


@pytest.mark.parametrize("policy", [None, False, "NONE"])
def test_client_cannot_turn_off_minimum_semantic_unit(django_user_model, policy):
    _, patient, document, _, fields = _graph(django_user_model, "path-export-policy-" + str(policy))
    with pytest.raises(ExportInputError):
        build_snapshot(patient, {**selection(document, fields["cps"]), "semantic_unit_policy": policy})


@pytest.mark.parametrize("change", ["drop_bundle", "unknown_schema", "drop_specimen"])
def test_portable_reader_rejects_new_pathology_without_its_required_semantics(django_user_model, change):
    _, patient, document, _, fields = _graph(django_user_model, "path-export-reader-" + change)
    snapshot = build_snapshot(patient, selection(document, fields["cps"]))
    data = json.loads(json_bytes(snapshot))
    row = data["clinical_fields"][0]
    if change == "unknown_schema":
        row["schema_version"] = row["content"]["schema_version"] = "PATHOLOGY_IHC_UNKNOWN"
    elif change == "drop_bundle":
        row["content"].pop("semantic_qualifiers", None)
    else:
        row["content"].setdefault("semantic_qualifiers", {}).pop("specimen_scope", None)
    with pytest.raises(ExportInputError):
        read_structured_data(json.dumps(data))


def test_unselected_context_correction_and_undo_invalidate_every_old_selected_snapshot(django_user_model):
    _, patient, document, _, fields = _graph(django_user_model, "path-export-stale")
    snapshot = build_snapshot(patient, selection(document, fields["cps"]))
    review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-CLONE-CHANGED"}, "raw_value": "SYN-CLONE-CHANGED"})
    for action in (None, "UNDO"):
        if action:
            review(patient, fields["clone"], action)
        with pytest.raises(SnapshotChanged):
            assert_snapshot_current(patient, snapshot)
        assert not effective_fact(Fact.objects.get(pk=fields["cps"].pk))["usable"]
        with pytest.raises(ExportInputError):
            build_snapshot(patient, selection(document, fields["cps"]))
