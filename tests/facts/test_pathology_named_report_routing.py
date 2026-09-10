"""Synthetic report structures, never fixtures copied from real gold values."""
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_extraction import extract


def named_report_rows(*, title="PD-L1免疫组化检测（SYN-CLONE）", marker="PD-L1蛋白表达水平",
                      antibody_heading="检测抗体", supplied_diagnosis=False):
    items = [
        (title, (.2, .04, .8, .08)),
        ("标本编号：SYN-ROUTING", (.05, .11, .45, .14)),
        ("检测项目：PD-L1免疫组化", (.05, .16, .45, .19)),
    ]
    if supplied_diagnosis:
        items += [
            ("受检者基本信息", (.05, .22, .4, .25)),
            ("样本基本信息", (.55, .22, .95, .25)),
            ("病理诊断：合成送检诊断", (.05, .27, .4, .3)),
            ("样本类型：组织切片", (.55, .27, .95, .3)),
            ("注：以上受检者基本信息由送检者提供，非本次检测结果。", (.05, .32, .95, .35)),
        ]
    items += [
        ("检测结果", (.05, .38, .2, .41)),
        ("检测项目", (.05, .44, .22, .47)),
        ("检测方法", (.3, .44, .44, .47)),
        (antibody_heading, (.52, .44, .68, .47)),
        ("检测结果", (.77, .44, .96, .47)),
        ("TPS：１７％", (.77, .5, .96, .53)),
        (marker, (.05, .54, .23, .57)),
        ("IHC", (.3, .54, .44, .57)),
        ("SYN-CLONE", (.52, .54, .68, .57)),
        ("CPS：２９", (.77, .58, .96, .61)),
        ("质量控制", (.05, .66, .3, .69)),
        ("PD-L1", (.05, .72, .22, .75)),
        ("TPS：100%", (.77, .72, .96, .75)),
    ]
    return [block(text, order=i, box=box) for i, (text, box) in enumerate(items)]


@pytest.mark.parametrize("title", [
    "PD-L1免疫组化检测(SYN-CLONE)",
    "PD-L1 免疫组化检测（SYN-CLONE）",
    "PD-L1免疫组织化学检测（SYN-2）",
    "PD-L1(SYN-CLONE)免疫组化检测",
])
def test_named_report_qualifier_preserves_title_and_routes_literal_results(title):
    rows = [block(title, order=0),
            block("标本编号：SYN-A", order=1, box=(.05, .25, .5, .28)),
            block("检测结果：PD-L1 TPS：17%", order=2, box=(.05, .4, .9, .43))]
    segments, unknown, groups = extract(rows)
    assert len(segments) == 1 and not unknown
    assay = next(field for field in groups[0] if field.key == "assay.identity")
    assert assay.value == {"label": title, "raw": title}
    assert [(piece.block.pk, piece.start, piece.end, piece.text) for piece in assay.fragments] == [
        (rows[0].pk, 0, len(title), title),
    ]
    assert len([field for field in groups[0] if field.key == "ihc.score"]) == 1


@pytest.mark.parametrize("title", [
    "病史记载：PD-L1免疫组化检测(SYN-CLONE)",
    "送检建议：PD-L1免疫组化检测(SYN-CLONE)",
    "PD-L1免疫组化检测(判读说明)",
    "PD-L1免疫组化检测(SYN-CLONE)的判读说明",
    "PD-L1免疫组化检测(SYN-CLONE)。建议复查",
    "PD-L1免疫组化检测(SYN-CLONE",
    "PD-L1免疫组化检测(SYN:CLONE)",
])
def test_qualified_assay_mention_without_standalone_title_remains_unparsed(title):
    segments, unknown, groups = extract([block(title + "\n检测结果：PD-L1 TPS：17%")])
    assert segments == [] and unknown == {1} and groups == []


@pytest.mark.parametrize("marker,antibody_heading", [
    ("PD-L1", "检测抗体"),
    ("PD-L1蛋白表达水平", "克隆号"),
    ("PD-L1 蛋白表达水平", "检测抗体"),
])
def test_explicit_result_columns_bind_marker_descriptor_antibody_and_both_scales(marker, antibody_heading):
    rows = named_report_rows(title="免疫组化检测报告单", marker=marker, antibody_heading=antibody_heading)
    original = deepcopy([(row.text, row.polygon) for row in rows])
    _, _, groups = extract(rows)
    fields = groups[0]
    scores = [field for field in fields if field.key == "ihc.score"]
    assert [(field.value["score_kind"], field.value["values"], field.value["unit"], field.raw_value)
            for field in scores] == [("TPS", ["17"], "%", "TPS：１７％"), ("CPS", ["29"], None, "CPS：２９")]
    by_id = {field.node_id: field for field in fields}
    marker_field = by_id[scores[0].links["MARKER"]]
    assert marker_field.value == {"code": "PD_L1", "label": "PD-L1", "raw": "PD-L1"}
    assert all(score.links == scores[0].links for score in scores)
    assert next(field for field in fields if field.key == "assay.antibody").value == {"text": "SYN-CLONE"}
    assert next(field for field in fields if field.key == "assay.method").value == {"code": "IHC", "raw": "IHC"}
    assert [(row.text, row.polygon) for row in rows] == original
    assert marker_field.fragments[0].text == "PD-L1"
    assert marker_field.fragments[0].end == 5


@pytest.mark.parametrize("marker", ["PD-L1-AS1蛋白表达水平", "PD-L10蛋白表达水平", "非PD-L1蛋白表达水平", "SYN-MARKER蛋白表达水平"])
def test_expression_descriptor_never_shortens_unknown_or_different_marker(marker):
    _, _, groups = extract(named_report_rows(title="免疫组化检测报告单", marker=marker, antibody_heading="克隆号"))
    assert not [field for field in groups[0] if field.key in {"ihc.marker", "ihc.score"}]


def test_unrecognized_table_row_still_prevents_borrowing_scores_for_known_descriptor():
    rows = named_report_rows(title="免疫组化检测报告单", antibody_heading="克隆号")
    rows.insert(-3, block("SYN-MARKER蛋白表达水平", order=30, box=(.05, .58, .23, .61)))
    _, _, groups = extract(rows)
    assert not [field for field in groups[0] if field.key == "ihc.score"]


@pytest.mark.parametrize("gallery_heading", ["检测图谱", "染色图像"])
def test_image_gallery_ends_table_before_unlabeled_caption_scores(gallery_heading):
    rows = named_report_rows(title="免疫组化检测报告单", antibody_heading="克隆号")
    rows[-3].text = gallery_heading
    rows[-1].text = "CPS：77"
    _, _, groups = extract(rows)
    assert [(field.value["score_kind"], field.value["values"]) for field in groups[0] if field.key == "ihc.score"] == [
        ("TPS", ["17"]), ("CPS", ["29"]),
    ]


@pytest.mark.parametrize("heading", ["检测图谱", "染色图像", "样本质控结果"])
def test_excluded_section_caption_does_not_become_an_inline_current_result(heading):
    rows = named_report_rows(title="免疫组化检测报告单", antibody_heading="克隆号")[:-3]
    rows += [block(heading, order=30, box=(.05, .66, .3, .69)),
             block("PD-L1 TPS：100%", order=31, box=(.05, .72, .96, .75))]
    _, _, groups = extract(rows)
    assert [(field.value["score_kind"], field.value["values"]) for field in groups[0] if field.key == "ihc.score"] == [
        ("TPS", ["17"]), ("CPS", ["29"]),
    ]


def test_quality_control_before_explicit_result_section_does_not_hide_current_table():
    rows = named_report_rows(title="免疫组化检测报告单", antibody_heading="克隆号")
    rows[3:3] = [block("样本质控结果", box=(.05, .23, .3, .26)),
                 block("PD-L1 TPS：100%", box=(.05, .29, .96, .32))]
    for order, row in enumerate(rows):
        row.reading_order = order
    _, _, groups = extract(rows)
    assert [(field.value["score_kind"], field.value["values"]) for field in groups[0] if field.key == "ihc.score"] == [
        ("TPS", ["17"]), ("CPS", ["29"]),
    ]


def test_supplied_patient_diagnosis_is_not_current_histology_but_sample_metadata_remains():
    _, _, groups = extract(named_report_rows(title="免疫组化检测报告单", marker="PD-L1",
                                            antibody_heading="克隆号", supplied_diagnosis=True))
    assert not [field for field in groups[0] if field.key == "specimen.histology"]
    assert next(field for field in groups[0] if field.key == "specimen.description").value == {"text": "组织切片"}
    assert len([field for field in groups[0] if field.key == "ihc.score"]) == 2


def test_explicit_current_diagnosis_section_after_supplied_information_is_still_current():
    rows = named_report_rows(title="免疫组化检测报告单", marker="PD-L1",
                             antibody_heading="克隆号", supplied_diagnosis=True)
    rows.insert(8, block("诊断结果\n病理诊断：合成本次组织学所见", order=8, box=(.05, .36, .95, .375)))
    for order, row in enumerate(rows):
        row.reading_order = order
    _, _, groups = extract(rows)
    assert [field.value for field in groups[0] if field.key == "specimen.histology"] == [
        {"text": "合成本次组织学所见", "assertion": "SOURCE_TEXT_ONLY_NOT_DIAGNOSED"},
    ]


@pytest.mark.django_db(transaction=True)
def test_real_upload_entry_publishes_named_table_fields_with_original_unicode_and_context(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    from apps.processing.models import ParsingVersion
    from apps.processing.ocr.fake import FixtureOcrProvider
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.processing.runner import ExecutionState, run_processing
    from apps.processing.value_objects import OcrRegion
    from tests.processing.test_pipeline import _document_and_run, _ocr_page, _png_bytes, _Store

    rows = named_report_rows(supplied_diagnosis=True)
    document, run = _document_and_run(django_user_model)
    page = replace(_ocr_page(), regions=tuple(OcrRegion(row.text, row.polygon, .98, i) for i, row in enumerate(rows)))
    pipeline = DocumentProcessingPipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))
    result = run_processing(run.pk, pipeline)
    assert result.state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.status == "PUBLISHED"
    assert version.clinical_extraction.status == "EXTRACTED"
    reports = list(version.clinical_reports.all())
    assert len(reports) == 1 and reports[0].routing_kind == "PATHOLOGY"
    scores = list(version.facts.filter(field_key="ihc.score").order_by("reading_order"))
    assert [field.automatic_content["value"]["values"] for field in scores] == [["17"], ["29"]]
    assert not version.facts.filter(field_key="specimen.histology").exists()
    for field in scores:
        effective = effective_field(field)
        assert effective["source_valid"] and effective["status"] == "PENDING"
        for fragment in field.source_fragments.select_related("ocr_block"):
            original = fragment.ocr_block
            assert original.parsing_version_id == version.pk
            assert fragment.raw_text == original.text[fragment.start_offset:fragment.end_offset]
            assert fragment.polygon == original.polygon == rows[original.reading_order].polygon
            assert original.text == rows[original.reading_order].text
