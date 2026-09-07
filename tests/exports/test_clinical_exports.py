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
    assert snapshot["schema_version"] == "1.1"
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
