from apps.exports.content import SCHEMA_VERSION
from copy import deepcopy
import csv
import io
import json
import zipfile

import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import SnapshotChanged
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from tests.documents.fakes import InMemoryObjectStore
from tests.facts.test_clinical_foundation import CT, clinical_fixture


pytestmark = pytest.mark.django_db


def _confirm(patient, document):
    for fact in document.facts.filter(representation="FIELD"):
        revise_fact(patient, fact.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(fact)["current_source_token"], checked_original=True)


def test_clinical_fields_enter_card_json_linked_csv_and_zip_with_old_tables_readable(django_user_model):
    from apps.exports.formats import build_artifact, csv_tables, json_bytes, read_structured_data
    from apps.exports.pdf import card_sections

    _, patient, document, _, _ = clinical_fixture(django_user_model, name="clinical-exports")
    _confirm(patient, document)
    snapshot = build_snapshot(patient, {"mode": "all", "details": True})
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["clinical_fields"]
    assert len(snapshot["clinical_reports"]) == 1
    data = json.loads(json_bytes(snapshot))
    assert data["clinical_fields"][0]["report_id"] == data["clinical_reports"][0]["id"]
    assert all(s["fact_id"] in {f["id"] for f in data["clinical_fields"]} for s in data["clinical_field_sources"])
    assert "current_source_token" not in json_bytes(snapshot).decode()
    tables = csv_tables(snapshot)
    assert {"clinical_reports.csv", "clinical_fields.csv", "clinical_field_sources.csv"} <= set(tables)
    assert next(csv.reader(io.StringIO(tables["facts.csv"].decode("utf-8-sig")))) == [
        "id", "document_id", "page", "source_id", "parsing_version", "category", "text", "content",
        "origin", "status", "revision_number", "revision_id",
    ]
    assert any("12mm" in entry.get("text", "") for section in card_sections(snapshot) for entry in section["entries"])
    artifact = build_artifact(snapshot, {"format": "zip", "parts": ["json", "csv"]}, InMemoryObjectStore())
    with zipfile.ZipFile(io.BytesIO(artifact.payload)) as bundle:
        assert "csv/clinical_fields.csv" in bundle.namelist()
        assert json.loads(bundle.read("records.json"))["clinical_fields"]
    old = deepcopy(data)
    old["schema_version"] = "1.0"
    for key in ("clinical_reports", "clinical_fields", "clinical_field_sources"):
        old.pop(key)
    restored = read_structured_data(json.dumps(old))
    assert restored["facts"] == old["facts"] and restored["clinical_fields"] == []


def test_report_and_field_selection_do_not_export_sibling_report_content(django_user_model):
    from apps.exports.formats import json_bytes

    _, patient, document, _, _ = clinical_fixture(django_user_model, name="clinical-scope",
        texts=CT + ["MR诊断报告书", "检查日期：2026-08-19 检查项目：颅脑磁共振", "影像表现：右额叶见结节，大小3×2mm。", "诊断意见：第二报告私有正文标记。"])
    _confirm(patient, document)
    first = document.clinical_reports.order_by("ordinal").first()
    field = first.fields.get(field_key="lesion.dimensions", automatic_content__value__components__0__value="12")
    snapshot = build_snapshot(patient, {"mode": "documents", "document_ids": [str(document.pk)], "clinical_field_ids": [str(field.pk)]})
    payload = json_bytes(snapshot).decode()
    assert "第二报告私有正文标记" not in payload and "右额叶" not in payload
    assert len(snapshot["clinical_fields"]) == len(snapshot["clinical_reports"]) == 1
    assert snapshot["clinical_reports"][0]["id"] == str(first.pk)
    assert snapshot["original_scope_warning"]


def test_field_revision_and_parent_exclusion_invalidate_frozen_snapshot(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import revise_report

    _, patient, document, _, _ = clinical_fixture(django_user_model, name="clinical-export-revoke")
    _confirm(patient, document)
    snapshot = build_snapshot(patient, {"mode": "all"})
    field = document.facts.get(field_key="imaging.impression")
    revise_fact(patient, field.pk, actor=patient.account, action="CORRECT", expected_revision=1,
                expected_source=effective_fact(field)["current_source_token"], checked_original=True,
                changes={"value": {"text": "更正后的结论"}, "raw_value": "更正后的结论"})
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, snapshot)
    fresh = build_snapshot(patient, {"mode": "all"})
    report = document.clinical_reports.get()
    revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE", expected_revision=0,
                  expected_source=report_source_token(report))
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, fresh)
    assert build_snapshot(patient, {"mode": "all"})["clinical_fields"] == []


def test_conflicting_confirmed_dates_remain_separate_in_detail_card_and_structured_export(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token, review_reports
    from apps.facts.clinical_services import add_manual_clinical_field

    client, patient, document, _, _ = clinical_fixture(django_user_model, name="date-conflicts")
    _confirm(patient, document)
    report = document.clinical_reports.get()
    extra = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk,
                                     entity_key="report", field_key="report.exam_date", value={"value": "2026-08-18", "precision": "DAY"},
                                     fragments=[{"page_number": 1, "raw_text": "检查日期：2026-08-18"}],
                                     expected_report_source=report_source_token(report))
    revise_fact(patient, extra.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(extra)["current_source_token"], checked_original=True)
    current = review_reports(patient, actor=patient.account)[0]
    assert current["date_conflict"] and len(current["date_values"]) == 2
    snapshot = build_snapshot(patient, {"mode": "all", "details": True})
    dates = [f for f in snapshot["clinical_fields"] if f["field_key"] == "report.exam_date"]
    assert len(dates) == 2 and all(f["conflict"] for f in dates)
    page = client.get(f"/facts/reports/{report.pk}/")
    assert "多个已核对检查日期存在冲突" in page.content.decode()


def _all_structured_payloads(snapshot, store=None, parts=None):
    from apps.exports.formats import build_artifact, csv_tables, json_bytes

    payloads = [json_bytes(snapshot).decode(), *[body.decode("utf-8-sig") for body in csv_tables(snapshot).values()]]
    artifact = build_artifact(snapshot, {"format": "zip", "parts": parts or ["json", "csv"]}, store or InMemoryObjectStore())
    with zipfile.ZipFile(io.BytesIO(artifact.payload)) as bundle:
        payloads.extend(bundle.read(name).decode("utf-8-sig") for name in bundle.namelist() if name.endswith((".json", ".csv")))
    return "\n".join(payloads), artifact


@pytest.mark.parametrize("selection_kind", ["report_ids", "clinical_field_ids"])
@pytest.mark.parametrize("include_legacy", [False, None, True])
def test_fine_clinical_scope_keeps_only_explicit_legacy_rows_in_json_csv_zip(django_user_model, selection_kind, include_legacy):
    from apps.facts.revisions import add_manual_fact
    from apps.labs.models import LabObservation
    from apps.processing.models import SourceEvidence

    marker = "UNSELECTED_SIBLING_REPORT_BODY"
    texts = CT + ["MR诊断报告书", "检查日期：2026-08-19 检查项目：颅脑磁共振",
                  "影像表现：右额叶见结节，大小3×2mm。", "诊断意见：" + marker + "。"]
    _, patient, document, version, _ = clinical_fixture(django_user_model, texts=texts, name="clinical-fine-legacy")
    report = document.clinical_reports.order_by("ordinal").first()
    selected = report.fields.get(field_key="lesion.dimensions", automatic_content__value__components__0__value="12")
    legacy = add_manual_fact(patient, document.pk, actor=patient.account, page_number=1, category="IMAGING", text=marker)
    for candidate in (selected, legacy):
        revise_fact(patient, candidate.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(candidate)["current_source_token"], checked_original=True)
    labs = []
    for index, name in enumerate(("EXPLICIT_LAB_VALUE", "NEVER_SELECTED_LAB_VALUE")):
        evidence = SourceEvidence.objects.create(parsing_version=version, document_page=document.pages.first(),
                                                  source_text=name, confidence="0.98")
        labs.append(LabObservation.objects.create(
            parsing_version=version, document_page=document.pages.first(), evidence=evidence, reading_order=index,
            raw_name=name, standard_code="LAB_WBC", standard_name=name, raw_value=str(index + 4), result_type="NUMERIC",
            raw_unit="10^9/L", capability_level="STABLE", dictionary_version="1.0.0", specimen="BLOOD"))
    scope = {"mode": "documents", "document_ids": [str(document.pk)],
             selection_kind: [str(selected.pk if selection_kind == "clinical_field_ids" else report.pk)]}
    if include_legacy:
        scope.update(fact_ids=[str(legacy.pk)], observation_ids=[str(labs[0].pk)])
    elif include_legacy is None:
        scope.update(fact_ids=None, observation_ids=None)
    snapshot = build_snapshot(patient, scope)
    payload, _ = _all_structured_payloads(snapshot)
    assert "12mm" in payload
    assert (marker in payload) is bool(include_legacy)
    assert ("EXPLICIT_LAB_VALUE" in payload) is bool(include_legacy)
    assert "NEVER_SELECTED_LAB_VALUE" not in payload
    assert_snapshot_current(patient, snapshot)
    whole = build_snapshot(patient, {"mode": "documents", "document_ids": [str(document.pk)]})
    assert len(whole["facts"]) == 1 and len(whole["labs"]) == 2


def test_single_field_export_does_not_smuggle_same_clause_content_or_report_spans(django_user_model):
    texts = list(CT)
    texts[2] = "影像表现：左肺上叶见结节，UNSELECTED_SAME_CLAUSE_NOTE，大小约987.321mm。"
    _, patient, document, _, _ = clinical_fixture(django_user_model, texts=texts, name="clinical-fine-context")
    selected = document.facts.get(field_key="lesion.site")
    revise_fact(patient, selected.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(selected)["current_source_token"], checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "all", "clinical_field_ids": [str(selected.pk)]})
    payload, _ = _all_structured_payloads(snapshot)
    assert "左肺上叶" in payload and "UNSELECTED_SAME_CLAUSE_NOTE" not in payload and "987.321" not in payload
    assert snapshot["clinical_reports"][0]["spans"] == []
    assert all(source["raw_text"] == "" for source in snapshot["clinical_field_sources"])
    assert snapshot["clinical_fields"][0]["source"]["polygon"]


@pytest.mark.parametrize("include_original", [False, True])
def test_fine_selection_original_switch_preserves_complete_original_only_when_selected(django_user_model, include_original):
    import hashlib
    from apps.documents.models import Document

    texts = list(CT)
    texts[2] = "影像表现：左肺上叶见结节，UNSELECTED_ORIGINAL_ONLY_TEXT，大小约987.321mm。"
    _, patient, document, _, _ = clinical_fixture(django_user_model, texts=texts, name="clinical-fine-original")
    original = ("SYNTHETIC_COMPLETE_ORIGINAL\n" + "\n".join(texts)).encode("utf-8")
    identity = hashlib.sha256(original).hexdigest()
    Document.objects.filter(pk=document.pk).update(sha256=identity, byte_size=len(original))
    document.refresh_from_db()
    selected = document.facts.get(field_key="lesion.site")
    revise_fact(patient, selected.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(selected)["current_source_token"], checked_original=True)
    snapshot = build_snapshot(patient, {"mode": "documents", "document_ids": [str(document.pk)],
                                        "clinical_field_ids": [str(selected.pk)]})
    assert snapshot["original_scope_warning"]
    store = InMemoryObjectStore()
    staged = store.put_staging(io.BytesIO(original), expected_size=len(original), expected_sha256=identity)
    store.promote_immutable(staged, document.original_object_key)
    parts = ["json", "csv"] + (["originals"] if include_original else [])
    payload, artifact = _all_structured_payloads(snapshot, store, parts)
    assert "UNSELECTED_ORIGINAL_ONLY_TEXT" not in payload and "987.321" not in payload
    with zipfile.ZipFile(io.BytesIO(artifact.payload)) as bundle:
        names = [name for name in bundle.namelist() if name.startswith("originals/")]
        assert len(names) == int(include_original)
        if include_original:
            assert bundle.read(names[0]) == original
            entry = next(row for row in json.loads(bundle.read("manifest.json"))["files"] if row["kind"] == "original")
            assert entry["sha256"] == identity and entry["byte_size"] == len(original)
