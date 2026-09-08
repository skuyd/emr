"""Synthetic reports exercise the persisted clinical extraction path."""

from copy import deepcopy

import pytest

from apps.facts.models import Fact, FactRevision
from apps.facts.readmodels import effective_fact
from tests.facts.test_clinical_foundation import clinical_fixture
from tests.facts.test_imaging_quantitative import fields, imaging


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(("body", "expected_sites"), [
    ("右肾不可见无强化囊性灶。", []),
    ("右肾未明确显示无强化囊性灶。", []),
    ("右肾未能明确显示无强化囊性灶。", []),
    ("右肾不能清楚地见无强化囊性灶。", []),
    ("右肾未明确可见无强化囊性灶。", []),
    ("右肾没有显示无强化囊性灶。", []),
    ("右肾无明确显示无强化囊性灶。", []),
    ("肝内未明确显示无强化囊性灶，增强后右肾可见无强化囊性灶。", ["右肾"]),
])
def test_complete_negated_observation_does_not_become_an_affirmative_substring(
    django_user_model, body, expected_sites,
):
    _, _, document, _, run = imaging(django_user_model, body)
    assert run.status == "EXTRACTED"
    site_fields = fields(document, "lesion.site")
    assert [field.automatic_content["value"]["text"] for field in site_fields] == expected_sites
    for field in site_fields:
        row = effective_fact(field)
        assert row["status"] == "PENDING" and row["source_valid"] and not row["usable"]
        assert field.raw_text == "增强后右肾可见无强化囊性灶。"
    assert not FactRevision.objects.filter(fact__document=document).exists()


def test_complete_negative_predicate_across_original_blocks_keeps_ocr_unchanged(django_user_model):
    texts = ["CT诊断报告书", "影像表现：Ⅲ、右肾未明", "确显示无强化囊性灶。", "诊断意见：请核对原件。"]
    _, _, document, version, _ = clinical_fixture(django_user_model, texts=texts, name="negative-predicate-source")
    assert not document.facts.filter(field_key__startswith="lesion.").exists()
    assert list(version.ocr_blocks.order_by("reading_order").values_list("text", flat=True)) == texts
    assert not FactRevision.objects.filter(fact__document=document).exists()


@pytest.mark.parametrize(("body", "expected_sites"), [
    ("右肾见无强化囊性灶。", ["右肾"]),
    ("右肾可见无明显强化囊性灶。", ["右肾"]),
    ("右肾显示无强化囊性灶。", ["右肾"]),
    ("右肾未见无强化囊性灶。", []),
    ("右肾未见明显强化结节。", []),
    ("右肾见囊性灶，未见强化。", ["右肾"]),
    ("右肾见无强化囊性灶，左肾未见囊性灶。", ["右肾"]),
    ("左肾未见囊性灶，右肾见无强化囊性灶。", ["右肾"]),
])
def test_reported_non_enhancing_focus_is_not_the_absence_of_that_focus(
    django_user_model, body, expected_sites,
):
    _, _, document, _, run = imaging(django_user_model, body)
    site_fields = fields(document, "lesion.site")
    assert run.status == "EXTRACTED"
    assert [field.automatic_content["value"]["text"] for field in site_fields] == expected_sites
    for field in site_fields:
        row = effective_fact(field)
        assert row["status"] == "PENDING" and row["source_valid"] and not row["usable"]
        assert field.revision_number == 0
        assert field.raw_text == "".join(fragment.raw_text for fragment in field.source_fragments.order_by("ordinal"))
    assert not FactRevision.objects.filter(fact__document=document).exists()


def test_affirmed_non_enhancing_focus_keeps_multiblock_original_source(django_user_model):
    texts = [
        "CT诊断报告书",
        "影像表现：Ⅲ、左肾未见异常；右肾见小",
        "片状无强化囊性",
        "灶。",
        "诊断意见：请核对原件。",
    ]
    _, _, document, version, _ = clinical_fixture(django_user_model, texts=texts, name="non-enhancing-source")
    field = Fact.objects.get(document=document, field_key="lesion.site")
    assert field.automatic_content["value"]["text"] == "右肾"
    assert field.raw_text == "右肾见小\n片状无强化囊性\n灶。"
    fragments = list(field.source_fragments.order_by("ordinal"))
    assert [(fragment.ocr_block.reading_order, fragment.start_offset, fragment.end_offset) for fragment in fragments] == [
        (1, texts[1].index("右肾"), len(texts[1])), (2, 0, len(texts[2])), (3, 0, len(texts[3])),
    ]
    for fragment in fragments:
        assert fragment.raw_text == fragment.ocr_block.text[fragment.start_offset:fragment.end_offset]
        assert fragment.polygon == fragment.ocr_block.polygon
        assert fragment.evidence.source_text == fragment.raw_text
        fragment.full_clean()
    assert list(version.ocr_blocks.order_by("reading_order").values_list("text", flat=True)) == texts


@pytest.mark.parametrize("phase", ["增强后", "增强扫描后"])
def test_phase_prefix_with_new_anatomy_keeps_two_distinct_persisted_observations(django_user_model, phase):
    _, _, document, _, _ = imaging(django_user_model,
        "肝内见点状致密影，" + phase + "双肾见囊性无强化灶。")
    first, second = fields(document, "lesion.site")
    assert [field.automatic_content["value"]["text"] for field in (first, second)] == ["肝内", "双肾"]
    assert first.entity_key != second.entity_key
    assert first.raw_text == "肝内见点状致密影，"
    assert second.raw_text == phase + "双肾见囊性无强化灶。"
    assert not FactRevision.objects.filter(fact__document=document).exists()


@pytest.mark.parametrize(("body", "expected_sites"), [
    ("肝内见点状致密影，增强后双肾未见囊性灶。", ["肝内"]),
    ("肝内未见异常，增强后右肾见无强化囊性灶。", ["右肾"]),
    ("左肾未见异常，右肾见结节，约7mm。", ["右肾"]),
])
def test_phase_boundary_keeps_local_negation_and_the_new_mapping_control(django_user_model, body, expected_sites):
    _, _, document, _, _ = imaging(django_user_model, body)
    actual = fields(document, "lesion.site")
    assert [field.automatic_content["value"]["text"] for field in actual] == expected_sites
    if body.startswith("左肾"):
        assert actual[0].raw_text == "右肾见结节，约7mm"


def test_phase_split_keeps_raw_unicode_and_original_offsets_across_blocks(django_user_model):
    texts = [
        "CT诊断报告书",
        "影像表现：左肺见Ⅲ类结节，增强",
        "后右肾见囊性无强化",
        "灶，大小１２×８ｍｍ。",
        "诊断意见：请核对原件。",
    ]
    _, _, document, version, _ = clinical_fixture(django_user_model, texts=texts, name="phase-prefix-source")
    originals = {block.pk: (block.text, deepcopy(block.polygon)) for block in version.ocr_blocks.all()}
    first, second = fields(document, "lesion.site")
    assert first.automatic_content["value"]["text"] == "左肺"
    assert first.raw_text == "左肺见Ⅲ类结节，"
    assert second.automatic_content["value"]["text"] == "右肾"
    assert second.raw_text == "增强\n后右肾见囊性无强化\n灶，大小１２×８ｍｍ"
    fragments = list(second.source_fragments.order_by("ordinal"))
    assert [(fragment.ocr_block.reading_order, fragment.start_offset, fragment.end_offset) for fragment in fragments] == [
        (1, texts[1].index("增强"), len(texts[1])), (2, 0, len(texts[2])), (3, 0, len(texts[3]) - 1),
    ]
    dimension = fields(document, "lesion.dimensions")[0]
    assert dimension.entity_key == second.entity_key
    assert dimension.automatic_content["value"]["raw"] == "１２×８ｍｍ"
    assert [part["value"] for part in dimension.automatic_content["value"]["components"]] == ["12", "8"]
    for field in (first, second, dimension):
        assert effective_fact(field)["status"] == "PENDING" and field.revision_number == 0
        for fragment in field.source_fragments.all():
            raw, polygon = originals[fragment.ocr_block_id]
            assert fragment.raw_text == raw[fragment.start_offset:fragment.end_offset]
            assert fragment.polygon == polygon
            assert fragment.evidence.source_text == fragment.raw_text
            fragment.full_clean()


def test_phase_scope_preserves_historical_measurements_suv_and_maximum_ownership(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "左肺见结节，原大小12×9mm，现大小8×6mm，既往检查SUVmax5.4，"
        "本次SUVmax3.2，为本报告最大病灶，增强后右肺见结节，约6mm，SUVmax2.1。",
        impression="左肺结节较前缩小；右肺结节同前。")
    site_fields = fields(document, "lesion.site")
    assert [field.automatic_content["value"]["text"] for field in site_fields] == ["左肺", "右肺"]
    left, right = [field.entity_key for field in site_fields]
    assert [(field.entity_key, field.automatic_content["value"]["measurement_role"],
             [part["value"] for part in field.automatic_content["value"]["components"]])
            for field in fields(document, "lesion.dimensions")] == [
        (left, "HISTORICAL", ["12", "9"]), (left, "CURRENT", ["8", "6"]), (right, "CURRENT", ["6"]),
    ]
    assert [(field.entity_key, field.automatic_content["value"]["measurement_role"],
             field.automatic_content["value"]["values"]) for field in fields(document, "lesion.suvmax")] == [
        (left, "HISTORICAL", ["5.4"]), (left, "CURRENT", ["3.2"]), (right, "CURRENT", ["2.1"]),
    ]
    assert [(field.entity_key, field.automatic_content["value"]["code"])
            for field in fields(document, "lesion.maximum_scope")] == [(left, "REPORT_MAXIMUM")]
    for field in document.facts.filter(field_key__startswith="lesion."):
        assert ("右肺" not in field.raw_text) if field.entity_key == left else ("左肺" not in field.raw_text)
    assert [field.automatic_content["value"]["text"] for field in fields(document, "comparison.statement")] == [
        "左肺结节较前缩小；右肺结节同前。",
    ]
