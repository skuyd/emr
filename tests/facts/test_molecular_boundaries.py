from tests.facts.test_clinical_segments import block
from tests.facts.test_molecular_extraction import extract

import pytest


def rows(body):
    return [block("基因检测报告\n标本编号：SYN-S\n检测名称：SYN-NGS\n体细胞变异检测结果\n" + body)]


def test_same_gene_different_transcript_site_never_collapses_identity():
    _, _, groups = extract(rows("基因：SYN1；完整表达：c.12+1G>A；转录本：NM_SYN.2；变异丰度：1%\n"
                               "基因：SYN1；完整表达：c.12+2T>C；转录本：NM_SYN.3；变异丰度：2%"))
    identities = [c for c in groups[0] if c.key == "variant.identity"]
    assert len(identities) == 2 and identities[0].entity != identities[1].entity
    assert [c.value["transcripts"]["values"] for c in identities] == [["NM_SYN.2"], ["NM_SYN.3"]]


@pytest.mark.parametrize(("literal", "numbers", "comparator", "unit", "approx"), [
    ("0.00%", ["0.00"], "EQ", "%", False), ("约1.20-2.40%", ["1.20", "2.40"], "RANGE", "%", True),
    ("≤01.20", ["01.20"], "LE", None, False), ("无法计算", [], None, None, False),
])
def test_quantities_preserve_long_decimal_qualifiers_without_positive_inference(literal, numbers, comparator, unit, approx):
    _, _, groups = extract(rows("基因：SYN1；完整表达：c.12+1G>A；变异丰度：" + literal))
    value = next(c.value for c in groups[0] if c.key == "variant.allele_fraction")
    assert (value["values"], value["comparator"], value["unit"], value["approximate"]) == (numbers, comparator, unit, approx)
    assert value["assertion"] == "AS_REPORTED_NO_POSITIVITY_INFERRED"


def test_cnv_and_fusion_keep_own_identity_and_as_printed_partner_order():
    _, _, groups = extract(rows("基因：SYN1；拷贝数变化：扩增；拷贝数：08.0\n融合表达：SYNB::SYNA；融合方向：5'→3'；5'转录本：NM_B.3；3'断点：exon 5"))
    identities = [c.value for c in groups[0] if c.key == "variant.identity"]
    assert [v["kind"] for v in identities] == ["COPY_NUMBER", "FUSION"]
    fusion = identities[1]
    assert fusion["order_meaning"] == "FIVE_TO_THREE"
    assert [p["gene"]["raw"] for p in fusion["partners"]] == ["SYNB", "SYNA"]
    assert fusion["partners"][0]["transcripts"]["values"] == ["NM_B.3"]
    assert fusion["partners"][1]["breakpoints"]["values"] == ["exon 5"]


def test_unprinted_fusion_direction_is_not_inferred_from_separator():
    _, _, groups = extract(rows("融合表达：SYNB::SYNA"))
    fusion = next(c.value for c in groups[0] if c.key == "variant.identity")
    assert fusion["order_meaning"] == "AS_PRINTED_UNKNOWN" and fusion["status"] == "INCOMPLETE"


def test_drug_association_is_ordered_entire_set_and_direction_is_reported_only():
    body = "基因：SYN1；完整表达：c.12+1G>A\n融合表达：SYNB::SYNA；融合方向：5'→3'\n" \
           "药物：SYN-A 与 SYN-B；关联变异：c.12+1G>A 及 SYNB::SYNA；依据方向：耐药；证据等级：II；等级体系：SYN-GRADE-v2"
    _, _, groups = extract(rows(body))
    fields = groups[0]
    identities = [c for c in fields if c.key == "variant.identity"]
    drug = next(c for c in fields if c.key == "drug_evidence.drugs")
    assert drug.links["VARIANT"] == [c.node_id for c in identities]
    assert drug.value["names"] == ["SYN-A", "SYN-B"] and drug.value["relation"] == "AND"
    direction = next(c for c in fields if c.key == "drug_evidence.direction")
    assert direction.value["code"] == "REPORT_RESISTANCE" and direction.source_role == "REPORT_DRUG_EVIDENCE"
    level = next(c for c in fields if c.key == "drug_evidence.level")
    assert level.value["system"] == {"state": "PRINTED", "raw": "SYN-GRADE-v2"}


def test_gene_only_drug_reference_cannot_infer_one_of_two_distinct_variants():
    _, _, groups = extract(rows("基因：SYN1；完整表达：c.1A>T\n基因：SYN1；完整表达：c.2G>C\n药物：SYN-A；关联变异：SYN1；证据等级：II"))
    drug = next(c for c in groups[0] if c.key == "drug_evidence.drugs")
    assert drug.links["VARIANT"] is None and drug.association_raw is None


def test_partial_reference_to_one_known_and_one_unknown_variant_is_not_reduced_to_known_subset():
    _, _, groups = extract(rows("基因：SYN1；完整表达：c.1A>T\n药物：SYN-A；关联变异：c.1A>T 及 c.99G>C；证据等级：II"))
    drug = next(c for c in groups[0] if c.key == "drug_evidence.drugs")
    assert drug.links["VARIANT"] is None


def test_drug_reference_to_a_previous_panel_is_preserved_unlinked():
    _, _, groups = extract(rows("基因：SYN1；完整表达：c.1A>T\n检测名称：SYN-other-panel\n药物：SYN-A；关联变异：c.1A>T"))
    drug = next(c for c in groups[0] if c.key == "drug_evidence.drugs")
    assert drug.links["VARIANT"] is None


def test_control_metadata_and_result_sections_do_not_become_current_assay():
    _, _, groups = extract(rows("质控\n检测名称：SYN-control\nTMB：100 mut/Mb\n检测结果\nTMB：2 mut/Mb"))
    fields = groups[0]
    control = next(c for c in fields if c.key == "assay.identity" and c.value["raw"] == "SYN-control")
    tmb = [c for c in fields if c.key == "assay.tmb_value"]
    assert control.source_role == "QC" and tmb[0].source_role == "QC"
    assert tmb[1].source_role == "CURRENT_RESULT" and tmb[1].links["ASSAY"] is None


def test_scoped_negative_retains_detection_kinds_and_no_missing_variant_rows_are_synthesized():
    _, _, groups = extract(rows("检测范围结论：本范围未检出拷贝数改变；检测范围：SYN panel拷贝数范围；检测种类：拷贝数"))
    negative = next(c for c in groups[0] if c.key == "assay.negative_statement")
    assert negative.value["assertion"] == "NOT_DETECTED"
    assert negative.value["scope"]["state"] == "EXPLICIT"
    assert negative.value["scope"]["detection_kinds"] == [{"code": "COPY_NUMBER", "raw": "拷贝数"}]
    assert not any(c.key.startswith("variant.") for c in groups[0])


def test_actual_pdl1_ihc_assay_stays_separate_from_cd274_copy_number():
    _, _, groups = extract(rows("基因：CD274；拷贝数变化：增加；拷贝数：4\n检测名称：PD-L1免疫组化\n检测结果：PD-L1 TPS：13% CPS：21"))
    fields = groups[0]
    scores = [c for c in fields if c.key == "ihc.score"]
    assert [c.value["score_kind"] for c in scores] == ["TPS", "CPS"]
    by_id = {c.node_id: c for c in fields}
    assert all(by_id[c.links["ASSAY"]].value["raw"] == "PD-L1免疫组化" for c in scores)
    assert all(by_id[c.links["MARKER"]].value["code"] == "PD_L1" for c in scores)
    assert next(c for c in fields if c.key == "variant.copy_number").value["values"] == ["4"]


def test_missing_table_header_records_unresolved_scope_without_guessing_columns():
    segments, _, groups = extract(rows("SYN1|c.1A>T|18%"))
    assert not any(c.key.startswith("variant.") for c in groups[0])
    assert "unmapped_molecular_table_row" in segments[0].limitations


def test_parallel_report_lanes_do_not_share_sample_or_assay():
    source = [block("基因检测报告", order=0, box=(.03, .05, .44, .08)),
              block("分子检测报告", order=1, box=(.56, .05, .98, .08)),
              block("标本编号：SYN-A", order=2, box=(.03, .12, .44, .15)),
              block("标本编号：SYN-B", order=3, box=(.56, .12, .98, .15)),
              block("检测名称：SYN-A-panel", order=4, box=(.03, .19, .44, .22)),
              block("检测名称：SYN-B-panel", order=5, box=(.56, .19, .98, .22)),
              block("TMB：2mut/Mb", order=6, box=(.03, .26, .44, .29)),
              block("TMB：8mut/Mb", order=7, box=(.56, .26, .98, .29)),
              block("报告日期：2030-04-05", order=8, box=(.40, .34, .66, .37))]
    segments, _, groups = extract(source)
    assert len(segments) == 2
    assert all("parallel_report_unplaced_text" in segment.limitations for segment in segments)
    assert [next(c.value["values"] for c in fields if c.key == "assay.tmb_value") for fields in groups] == [["2"], ["8"]]
    assert not any(c.key == "assay.report_date" for fields in groups for c in fields)


def test_explicit_continuation_requires_same_report_id_and_repeated_table_header():
    first = block("分子检测报告\n报告编号：SYN-M1\n标本编号：SYN-S\n检测名称：SYN-NGS\n体细胞变异检测结果\n基因|完整表达|变异丰度\nSYN1|c.1A>T|1%", page=1)
    second = block("报告编号：SYN-M1\n第2页/共2页\n基因|完整表达|变异丰度\nSYN2|c.2G>C|2%", page=2)
    segments, unknown, groups = extract([first, second])
    assert len(segments) == 1 and not unknown
    assert [c.value["gene"]["raw"] for c in groups[0] if c.key == "variant.identity"] == ["SYN1", "SYN2"]
    second.text = second.text.replace("SYN-M1", "SYN-M2")
    segments, unknown, groups = extract([first, second])
    assert len(segments) == 1 and unknown == {2}
    assert [c.value["gene"]["raw"] for c in groups[0] if c.key == "variant.identity"] == ["SYN1"]


def test_exact_complete_overview_repeat_combines_sources_without_duplicate_identity():
    source = "基因|完整表达|编码位点|蛋白位点|密码子|转录本|位置|变异丰度\n" \
             "SYN1|c.1A>T|c.1A>T|p.?|codon 1|NM_SYN.2|build-X chr1:1|1%\n" \
             "SYN1|c.1A>T|c.1A>T|p.?|codon 1|NM_SYN.2|build-X chr1:1|1%"
    _, _, groups = extract(rows(source))
    identities = [c for c in groups[0] if c.key == "variant.identity"]
    metrics = [c for c in groups[0] if c.key == "variant.allele_fraction"]
    assert len(identities) == len(metrics) == 1
    assert len(metrics[0].fragments) == 2
    assert metrics[0].fragments[0].start != metrics[0].fragments[1].start
    assert metrics[0].links["VARIANT"] == identities[0].node_id


def test_explicit_assertion_stays_separate_from_numeric_quantity():
    _, _, groups = extract(rows("基因：SYN1；完整表达：c.1A>T；变异丰度：1%；结果断言：检出"))
    metric = next(c for c in groups[0] if c.key == "variant.allele_fraction")
    assert metric.assertion_code == "DETECTED" and metric.assertion_raw == "检出"
    assert metric.value["assertion"] == "AS_REPORTED_NO_POSITIVITY_INFERRED"
    assert all(p in metric.value_fragments for p in metric.assertion_fragments)


def test_unqualified_assertion_for_two_result_columns_is_not_applied_to_both():
    segments, _, groups = extract(rows("TMB：8mut/Mb；MSI：MSS；结果断言：检出"))
    assert "ambiguous_result_assertion" in segments[0].limitations
    assert all(c.assertion_code is None for c in groups[0])


def test_absent_ocr_component_is_unknown_even_with_an_otherwise_complete_table_row():
    _, _, groups = extract(rows("基因|完整表达|变异丰度\nSYN1|c.1A>T|1%"))
    identity = next(c.value for c in groups[0] if c.key == "variant.identity")
    assert identity["transcripts"] == {"state": "UNKNOWN", "values": []}
    assert identity["status"] == "INCOMPLETE"


def test_separate_label_value_boxes_keep_actual_sources_without_inserting_colons():
    source = [block("分子检测报告", order=0, box=(.05, .03, .95, .07)),
              block("标本编号", order=1, box=(.05, .12, .25, .16)), block("SYN-S", order=2, box=(.35, .12, .55, .16)),
              block("检测名称", order=3, box=(.05, .21, .25, .25)), block("SYN-NGS", order=4, box=(.35, .21, .55, .25)),
              block("TMB", order=5, box=(.05, .30, .25, .34)), block("8mut/Mb", order=6, box=(.35, .30, .55, .34))]
    _, _, groups = extract(source)
    fields = groups[0]
    metric = next(c for c in fields if c.key == "assay.tmb_value")
    assert metric.value["values"] == ["8"]
    assert metric.fragments[0].block.pk == source[6].pk
    assert metric.label_fragments[0].block.pk == source[5].pk
    by_id = {c.node_id: c for c in fields}
    assert by_id[metric.links["ASSAY"]].value["raw"] == "SYN-NGS"
    assert by_id[metric.links["SPECIMEN"]].value["raw"] == "SYN-S"


def test_unrecognized_drug_direction_is_preserved_unparsed_never_original_not_stated():
    segments, _, groups = extract(rows("药物：SYN-A；依据方向：特殊报告表达"))
    assert not any(c.key == "drug_evidence.direction" for c in groups[0])
    assert "unclassified_drug_direction" in segments[0].limitations
    assert "特殊报告表达" in "".join(p.text for p in segments[0].pieces)
