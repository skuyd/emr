import pytest

from tests.facts.test_clinical_segments import block
from tests.facts.test_pathology_extraction import extract


def fields(body):
    return extract([block("病理诊断报告书\n标本编号：SYN-H\n" + body)])[2][0]


def test_specimen_and_tumor_dimensions_remain_separate_with_literal_axes():
    from apps.facts.clinical_schema import validate_value

    output = fields("组织学诊断：原文组织学待核对\n标本大小：32×21×9mm\n肿瘤大小：约12×8mm\n临床诊断：体表肿块长径90mm")
    sizes = [field for field in output if field.key == "specimen.dimensions"]
    assert [(field.value["measurement_object"], [component["value"] for component in field.value["components"]]) for field in sizes] == [
        ("SPECIMEN", ["32", "21", "9"]), ("TUMOR", ["12", "8"]),
    ]
    assert sizes[1].value["approximate"]
    assert all(component["axis"] is None for field in sizes for component in field.value["components"])
    for field in sizes:
        validate_value(field.key, field.value)
        assert field.links["SPECIMEN"]


def test_explicit_nodes_per_group_do_not_sum_or_assign_stage():
    output = fields("组织学诊断：原文结果待核对\n淋巴结计数：组甲送检5枚，转移0枚；组乙送检7枚，转移2枚")
    item = next(field for field in output if field.key == "specimen.nodes")
    assert [(group["label"], group["sampled"], group["positive"]) for group in item.value["groups"]] == [("组甲", "5", "0"), ("组乙", "7", "2")]
    assert not any(field.key.endswith("reported_stage") for field in output)
    assert all(group["raw"] in item.raw_value for group in item.value["groups"])


def test_partial_node_counts_remain_null_instead_of_zero_or_fraction_convention():
    output = fields("组织学诊断：原文结果待核对\n淋巴结计数：组甲送检5枚；组乙转移2枚；组丙（0/4）")
    item = next(field for field in output if field.key == "specimen.nodes")
    assert [(group["label"], group["sampled"], group["positive"]) for group in item.value["groups"]] == [("组甲", "5", None), ("组乙", None, "2")]
    assert "unparsed_node_count_expression" in item.limitations


@pytest.mark.parametrize("body", ["临床诊断：腺癌\n送检诊断：浸润性癌", "标本大小：32×21×9\n淋巴结计数：0/4"])
def test_clinical_history_unknown_dimension_units_and_unlabeled_counts_are_not_inferred(body):
    output = fields("组织学诊断：原文待核对\n" + body)
    assert not any(field.key in {"specimen.dimensions", "specimen.nodes"} for field in output)
    histology = next(field for field in output if field.key == "specimen.histology")
    assert histology.value["text"] == "原文待核对"
