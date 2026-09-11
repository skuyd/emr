"""Synthetic domain evidence for review states and extracted report metadata."""
from copy import deepcopy

import pytest

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import json_bytes, read_structured_data
from apps.facts.clinical_readmodels import effective_field
from tests.exports.test_molecular_exports import selection
from tests.facts.molecular_factories import graph
from tests.facts.test_clinical_segments import block
from tests.facts.test_molecular_pipeline import confirm_report, fixture
from tests.facts.test_pathology_views import review_post

pytestmark = pytest.mark.django_db


def test_actual_molecular_defer_undo_confirm_controls_selected_output(django_user_model):
    client, patient, document, _, fields = graph(django_user_model, "molecular-defer-output")
    for key in ("specimen", "assay", "identity"):
        assert review_post(client, patient, fields[key]).status_code == 302
    field = fields["metric"]
    original = deepcopy(field.automatic_content)
    sources = list(field.source_fragments.values("id", "raw_text", "start_offset", "end_offset"))
    chosen = selection(document, field)

    def unavailable(status):
        row = effective_field(field)
        assert row["status"] == status and not row["usable"]
        with pytest.raises(ExportInputError):
            build_snapshot(patient, chosen)

    unavailable("PENDING")
    assert review_post(client, patient, field, action="DEFER").status_code == 302
    unavailable("DEFERRED")
    assert review_post(client, patient, field, action="UNDO").status_code == 302
    unavailable("PENDING")
    assert review_post(client, patient, field).status_code == 302
    assert effective_field(field)["status"] == "CONFIRMED" and effective_field(field)["usable"]
    confirmed = build_snapshot(patient, chosen)
    row, = read_structured_data(json_bytes(confirmed))["clinical_fields"]
    assert row["field_key"] == "variant.allele_fraction"
    assert row["content"]["value"]["values"] == ["01.20"]

    assert review_post(client, patient, field, action="DEFER").status_code == 302
    unavailable("DEFERRED")
    with pytest.raises(SnapshotChanged):
        assert_snapshot_current(patient, confirmed)
    # Unchanged original/context permits undo to restore the preceding confirmation.
    assert review_post(client, patient, field, action="UNDO").status_code == 302
    assert effective_field(field)["status"] == "CONFIRMED" and effective_field(field)["usable"]
    row, = read_structured_data(json_bytes(build_snapshot(patient, chosen)))["clinical_fields"]
    assert row["content"]["value"]["values"] == ["01.20"]
    field.refresh_from_db()
    assert field.automatic_content == original
    assert list(field.source_fragments.values("id", "raw_text", "start_offset", "end_offset")) == sources
    assert list(field.revisions.order_by("sequence").values_list("action", flat=True)) == [
        "DEFER", "UNDO", "CONFIRM", "DEFER", "UNDO",
    ]


def test_extracted_metadata_keeps_own_specimen_assay_dates_and_selected_values(django_user_model):
    rows = [block(text, order=i) for i, text in enumerate([
        "合成医院 分子检测报告", "报告编号：SYN-METADATA",
        "标本编号：SYN-S-A", "样本类型：合成组织甲", "检测名称：SYN-NGS-A",
        "Panel名称：SYN-PANEL-A；Panel规模：约0500个基因",
        "采样日期：2030年4月；收样日期：未提供；报告日期：2030-04-05",
        "标本编号：SYN-S-B", "样本类型：合成血浆乙", "检测名称：SYN-NGS-B",
        "Panel名称：SYN-PANEL-B；Panel规模：12个位点",
        "采样日期：2031年；收样日期：2031-06-07；报告日期：2031年6月",
    ])]
    patient, document, _, _ = fixture(django_user_model, rows, "molecular-metadata-output")
    report = document.clinical_reports.get()
    assert report.routing_kind == "MOLECULAR"
    originals = {f.pk: deepcopy(f.automatic_content) for f in report.fields.all()}
    confirm_report(patient, report)
    expected = {
        "SYN-NGS-A": {"specimen": "SYN-S-A", "sample": "合成组织甲", "panel": "SYN-PANEL-A",
                      "size": ["0500"], "approximate": True, "object": "基因",
                      "dates": {"collection_date": {"value": "2030-04", "precision": "MONTH"},
                                "received_date": {"value": None, "precision": "UNKNOWN"},
                                "report_date": {"value": "2030-04-05", "precision": "DAY"}}},
        "SYN-NGS-B": {"specimen": "SYN-S-B", "sample": "合成血浆乙", "panel": "SYN-PANEL-B",
                      "size": ["12"], "approximate": False, "object": "位点",
                      "dates": {"collection_date": {"value": "2031", "precision": "YEAR"},
                                "received_date": {"value": "2031-06-07", "precision": "DAY"},
                                "report_date": {"value": "2031-06", "precision": "MONTH"}}},
    }
    for assay in report.fields.filter(field_key="assay.identity"):
        want = expected[assay.automatic_content["value"]["raw"]]
        bound = list(report.fields.filter(entity_key=assay.entity_key))
        by_key = {f.field_key: f for f in bound}
        specimen_binding, = assay.automatic_content["entity_context"]["bindings"]
        specimen = report.fields.get(pk=specimen_binding["target_fact_id"])
        assert specimen.automatic_content["value"]["raw"] == want["specimen"]
        sample = report.fields.get(field_key="specimen.description", entity_key=specimen.entity_key)
        assert sample.automatic_content["value"] == {"text": want["sample"]}
        panel = by_key["assay.panel_name"]
        assert panel.automatic_content["value"] == {"text": want["panel"]}
        size = by_key["assay.panel_size"]
        expected_size = {"status": "PARSED", "values": want["size"], "comparator": "EQ",
                         "unit": "个", "unit_state": "PRINTED", "approximate": want["approximate"],
                         "measurement_kind": "PANEL_SIZE", "assertion": "AS_REPORTED_NO_POSITIVITY_INFERRED",
                         "count_object": {"state": "PRINTED", "raw": want["object"]}}
        assert {k: v for k, v in size.automatic_content["value"].items() if k != "raw"} == expected_size
        selected = [sample, panel, size]
        for role, date in want["dates"].items():
            field = by_key["assay." + role]
            assert field.automatic_content["value"] == date
            assert field.automatic_content["schema_version"] == "PATHOLOGY_IHC_V1"
            selected.append(field)
        for field in selected:
            assert effective_field(field)["usable"], (field.field_key, effective_field(field))
            for fragment in field.source_fragments.all():
                assert fragment.raw_text == fragment.ocr_block.text[fragment.start_offset:fragment.end_offset]
            if field != sample:
                targets = {b["role"]: b["target_fact_id"] for b in field.automatic_content["entity_context"]["bindings"]}
                assert targets == {"SPECIMEN": str(specimen.pk), "ASSAY": str(assay.pk)}
        public = read_structured_data(json_bytes(build_snapshot(patient, selection(document, *selected))))
        output = {r["field_key"]: r for r in public["clinical_fields"]}
        assert set(output) == {"specimen.description", "assay.panel_name", "assay.panel_size",
                               "assay.collection_date", "assay.received_date", "assay.report_date"}
        assert output["specimen.description"]["content"]["value"] == {"text": want["sample"]}
        assert output["assay.panel_name"]["content"]["value"] == {"text": want["panel"]}
        assert output["assay.panel_size"]["content"]["value"] == expected_size
        for role, date in want["dates"].items():
            assert output["assay." + role]["content"]["value"] == date
    assert report.fields.filter(field_key="assay.identity").count() == 2
    assert {f.pk: f.automatic_content for f in report.fields.all()} == originals
