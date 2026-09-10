"""An explicit column heading does not override its governing source section."""
from dataclasses import replace

import pytest

from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_extraction import extract
from tests.facts.test_pathology_named_report_routing import named_report_rows


def rows_for_section(section):
    rows = named_report_rows(title="免疫组化检测报告单")
    rows[3].text = section
    return rows


@pytest.mark.parametrize("section", ["样本质控结果", "质量控制", "阳性对照", "阴性对照", "检测图谱", "受检者基本信息"])
def test_excluded_section_keeps_its_complete_table_out_of_current_fields(section):
    segments, unknown, groups = extract(rows_for_section(section))
    assert len(segments) == 1 and not unknown
    assert not [field for field in groups[0] if field.key.startswith("ihc.")]
    assert next(field for field in groups[0] if field.key == "specimen.identity").value["raw"] == "SYN-ROUTING"


@pytest.mark.parametrize("shape", ["missing_method", "crossing_result_header"])
def test_incomplete_or_crossing_column_layout_never_promotes_excluded_inline_text(shape):
    rows = rows_for_section("样本质控结果")
    if shape == "missing_method":
        rows[5].text = "合成未知栏"
    else:
        rows[7].polygon = [[.04, .44], [.96, .44], [.96, .47], [.04, .47]]
    rows[8:13] = [block("PD-L1 TPS：18%", order=8, box=(.05, .54, .96, .57))]
    _, _, groups = extract(rows)
    assert not [field for field in groups[0] if field.key in {"ihc.marker", "ihc.score"}]


@pytest.mark.parametrize("section", ["样本质控结果", "检测图谱", "受检者基本信息"])
def test_only_a_new_independent_current_heading_reopens_result_extraction(section):
    rows = rows_for_section(section)
    rows.insert(4, block("检测结果", box=(.05, .405, .22, .425)))
    for order, row in enumerate(rows):
        row.reading_order = order
    _, _, groups = extract(rows)
    assert [(field.source_role, field.value["score_kind"], field.value["values"])
            for field in groups[0] if field.key == "ihc.score"] == [
        ("CURRENT_RESULT", "TPS", ["17"]), ("CURRENT_RESULT", "CPS", ["29"]),
    ]


def test_default_current_table_without_separate_section_heading_remains_supported():
    rows = named_report_rows(title="免疫组化检测报告单")
    del rows[3]
    _, _, groups = extract(rows)
    assert [(field.value["score_kind"], field.value["values"])
            for field in groups[0] if field.key == "ihc.score"] == [("TPS", ["17"]), ("CPS", ["29"])]


def test_excluded_table_ends_before_a_later_current_inline_result():
    rows = rows_for_section("样本质控结果")[:-3]
    rows += [block("检测结果", order=30, box=(.05, .7, .25, .73)),
             block("PD-L1 CPS：31", order=31, box=(.05, .8, .96, .83))]
    _, _, groups = extract(rows)
    assert [(field.value["score_kind"], field.value["values"])
            for field in groups[0] if field.key == "ihc.score"] == [("CPS", ["31"])]


@pytest.mark.parametrize("section", ["样本质控结果", "检测图谱", "受检者基本信息"])
def test_value_label_inside_excluded_section_is_not_a_new_current_heading(section):
    rows = named_report_rows(title="免疫组化检测报告单")[:3]
    rows += [block(section, order=3, box=(.05, .38, .3, .41)),
             block("检测结果：PD-L1 TPS：100%", order=4, box=(.05, .5, .96, .53))]
    _, _, groups = extract(rows)
    assert not [field for field in groups[0] if field.key.startswith("ihc.")]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("section,expected", [("样本质控结果", []), ("检测结果", [["17"], ["29"]])])
def test_actual_upload_never_commits_excluded_table_as_current(django_user_model, section, expected):
    from apps.processing.models import ParsingVersion
    from apps.processing.ocr.fake import FixtureOcrProvider
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.processing.runner import ExecutionState, run_processing
    from apps.processing.value_objects import OcrRegion
    from tests.processing.test_pipeline import _document_and_run, _ocr_page, _png_bytes, _Store

    document, run = _document_and_run(django_user_model)
    rows = rows_for_section(section)
    page = replace(_ocr_page(), regions=tuple(OcrRegion(row.text, row.polygon, .98, index) for index, row in enumerate(rows)))
    pipeline = DocumentProcessingPipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.status == "PUBLISHED"
    assert version.clinical_extraction.status == "EXTRACTED"
    actual = [field.automatic_content["value"]["values"] for field in version.facts.filter(field_key="ihc.score").order_by("reading_order")]
    assert actual == expected
