"""Anonymous report literals: quantitative uptake is a reviewable source claim."""
from apps.facts.laterality import review_parent_arguments

from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from tests.facts.test_clinical_foundation import clinical_fixture


pytestmark = pytest.mark.django_db


def imaging(django_user_model, findings, *, impression="请结合原件核对。", name="quantitative"):
    return clinical_fixture(django_user_model, name=name, texts=[
        "合成医院 PET/CT诊断报告书",
        "检查日期：2026-08-17 检查项目：全身PET/CT",
        "影像表现：" + findings,
        "诊断意见：" + impression,
        "报告日期：2026-08-18 报告医师：合成医师",
    ])


def fields(document, key):
    return list(document.facts.filter(representation="FIELD", field_key=key).order_by("reading_order"))


def test_suv_before_or_after_dimensions_keeps_own_entity_and_original_unicode(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "左肺上叶见结节，约12×8mm，ＳＵＶｍａｘ约３．５。"
        "右肺下叶见肿块，SUVmax6.2，约18mm。肝脏背景SUVmax2.1。")
    suvs = fields(document, "lesion.suvmax")
    assert len(suvs) == 2
    assert [f.automatic_content["value"]["values"] for f in suvs] == [["3.5"], ["6.2"]]
    assert [f.automatic_content["value"]["approximate"] for f in suvs] == [True, False]
    for suv, dimension in zip(suvs, fields(document, "lesion.dimensions")):
        assert suv.entity_key == dimension.entity_key
        assert suv.automatic_content["value"]["unit"] is None
        for fragment in suv.source_fragments.all():
            assert fragment.raw_text == fragment.ocr_block.text[fragment.start_offset:fragment.end_offset]
            assert fragment.polygon == fragment.ocr_block.polygon
            fragment.full_clean()
    assert suvs[0].automatic_content["raw_value"] == "ＳＵＶｍａｘ约３．５"
    assert "左肺上叶" in suvs[0].raw_text and "右肺下叶" not in suvs[0].raw_text
    assert "肝脏背景" not in suvs[1].raw_text


@pytest.mark.parametrize("expression,numbers,comparator,unit,approximate", [
    ("SUVmax≤4.20", ["4.20"], "LE", None, False),
    ("SUVmax>0.8", ["0.8"], "GT", None, False),
    ("SUVmax约2.0～3.0", ["2.0", "3.0"], "RANGE", None, True),
    ("SUVmax=4.1g/mL", ["4.1"], "EQ", "g/mL", False),
])
def test_suv_qualifiers_and_explicit_unit_are_not_turned_into_exact_unqualified_values(
    django_user_model, expression, numbers, comparator, unit, approximate,
):
    _, _, document, _, _ = imaging(django_user_model, f"左肺见结节，约12mm，{expression}。")
    value = fields(document, "lesion.suvmax")[0].automatic_content["value"]
    assert value == dict(values=numbers, comparator=comparator, unit=unit, approximate=approximate,
                         measurement_role="CURRENT", raw=expression)


def test_historical_suv_is_literal_reference_and_not_a_second_current_measurement(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "左肺结节约12mm，本次SUVmax3.2，前片SUVmax5.4。")
    suvs = fields(document, "lesion.suvmax")
    assert len(suvs) == 2 and suvs[0].entity_key == suvs[1].entity_key
    assert [f.automatic_content["value"]["measurement_role"] for f in suvs] == ["CURRENT", "HISTORICAL"]
    assert "前片" in suvs[1].raw_text


@pytest.mark.parametrize("body", [
    "左肺见结节约12mm，SUVmean3.0。显像剂剂量4.2mCi，注射60min后检查。",
    "纵隔血池SUVmax2.3。肝脏背景SUVmax2.8。",
    "肝脏SUVmax2.8，作为参考本底。",
    "左肺结节约12mm，SUVmax未测定。",
])
def test_background_other_statistics_and_unmeasured_values_do_not_become_lesion_suv(django_user_model, body):
    _, _, document, _, _ = imaging(django_user_model, body)
    assert fields(document, "lesion.suvmax") == []


def test_named_abnormal_uptake_without_size_remains_pending_and_keeps_qualifying_text(django_user_model):
    from apps.facts.readmodels import effective_fact

    client, _, document, _, _ = imaging(django_user_model,
        "右肾上腺稍增粗，呈轻度放射性浓聚，SUVmax4.6。")
    suv = fields(document, "lesion.suvmax")[0]
    row = effective_fact(suv)
    assert row["status"] == "PENDING" and not row["usable"]
    assert "稍增粗" in suv.raw_text and "轻度" in suv.raw_text
    assert fields(document, "lesion.dimensions") == []
    assert "轻度" in client.get(f"/facts/{suv.pk}/").content.decode()


def test_comparison_statements_and_partial_reference_date_never_re_date_or_link_reports(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "对比前片（2025年06月）：左肺见结节约12mm。",
        impression="1、左肺结节较前缩小；右肺结节同前。2、建议三个月后复查。")
    statements = fields(document, "comparison.statement")
    dates = fields(document, "comparison.reference_date")
    assert len(statements) == 2 and len(dates) == 1
    assert statements[1].automatic_content["value"]["text"] == "左肺结节较前缩小；右肺结节同前。"
    assert dates[0].entity_key == statements[0].entity_key
    assert dates[0].automatic_content["value"] == {"value": "2025-06", "precision": "MONTH"}
    assert fields(document, "report.exam_date")[0].automatic_content["value"]["value"] == "2026-08-17"
    assert "comparison_is_literal_not_linked_examination" in dates[0].automatic_content["limitations"]
    assert not any("linked_report_id" in f.automatic_content for f in [*dates, *statements])


def test_larger_in_a_group_is_not_report_wide_maximum_and_size_does_not_imply_either(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "双肺多发结节，较大者位于左肺上叶，约12mm。"
        "右肺下叶结节约8mm，为本报告最大病灶。肝内见肿块约40mm。")
    maxima = fields(document, "lesion.maximum_scope")
    assert [f.automatic_content["value"]["code"] for f in maxima] == ["GROUP_LARGER", "REPORT_MAXIMUM"]
    assert maxima[0].entity_key != maxima[1].entity_key
    assert len(fields(document, "lesion.dimensions")) == 3


@pytest.mark.parametrize("body", [
    "左肺结节约12mm，较前增大。", "左肺结节约12mm，并非本次最大病灶。",
    "左肺结节约12mm，不能确定是否为最大病灶。",
])
def test_comparison_and_uncertain_maximum_do_not_create_definitive_maximum_flag(django_user_model, body):
    _, _, document, _, _ = imaging(django_user_model, body)
    assert fields(document, "lesion.maximum_scope") == []


def test_original_schema_confirmation_and_new_scalar_validation_coexist(django_user_model):
    from apps.facts.clinical_schema import field_content, validate_content
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact

    _, patient, document, _, _ = imaging(django_user_model, "左肺结节约12mm。")
    original = fields(document, "lesion.dimensions")[0]
    assert original.schema_version == "1.0"
    token = effective_fact(original)["current_source_token"]
    revise_fact(patient, original.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=token, checked_original=True, **review_parent_arguments(original))
    original.refresh_from_db()
    validate_content(original.automatic_content)
    assert effective_fact(original)["usable"]
    scalar = field_content("lesion.suvmax", dict(values=["4.0"], comparator="EQ", unit=None,
        approximate=False, measurement_role="CURRENT", raw="SUVmax4.0"), "SUVmax4.0")
    assert scalar["schema_version"] == "1.1"
    for values in ([True], ["NaN"], ["-1"], ["2", "1"], ["1", "2", "3"]):
        bad = deepcopy(scalar)
        bad["value"]["values"] = values
        with pytest.raises(ValidationError):
            validate_content(bad)
    assert effective_fact(original)["current_source_token"] == token
    assert effective_fact(original)["usable"]


@pytest.mark.parametrize("header,expected", [
    ("对比前片（日期不详）", {"value": None, "precision": "UNKNOWN"}),
    ("与2025年05月CT比较", {"value": "2025-05", "precision": "MONTH"}),
    ("对比2025-05-06检查", {"value": "2025-05-06", "precision": "DAY"}),
])
def test_explicit_reference_date_variants_keep_precision_without_inventing_a_link(django_user_model, header, expected):
    _, _, document, _, _ = imaging(django_user_model, header + "：左肺结节约12mm。")
    dates = fields(document, "comparison.reference_date")
    assert len(dates) == 1 and dates[0].automatic_content["value"] == expected
    assert dates[0].automatic_content["raw_value"] == header


def test_literal_impression_maximum_names_one_existing_report_observation(django_user_model):
    _, _, document, _, _ = imaging(django_user_model, "左肺上叶结节约12mm。右肺下叶结节约8mm。",
                                   impression="本次最大病灶为左肺上叶结节。")
    maximum = fields(document, "lesion.maximum_scope")
    assert len(maximum) == 1
    left = document.facts.get(field_key="lesion.site", automatic_content__value__text="左肺上叶")
    assert maximum[0].entity_key == left.entity_key
    assert maximum[0].raw_text == "本次最大病灶为左肺上叶结节。"


def test_ambiguous_same_site_maximum_in_impression_is_not_attached_to_one_of_two(django_user_model):
    _, _, document, _, _ = imaging(django_user_model, "左肺上叶结节约12mm。左肺上叶另见结节约8mm。",
                                   impression="本次最大病灶为左肺上叶结节。")
    assert fields(document, "lesion.maximum_scope") == []


def test_suv_range_correction_keeps_original_qualifiers_in_human_readable_value():
    from apps.facts.clinical_forms import ClinicalValueForm
    from apps.facts.clinical_schema import field_content

    form = ClinicalValueForm("lesion.suvmax", data={"raw_value": "SUVmax约2.1～3.4", "scalar_1": "2.1", "scalar_2": "3.4",
        "comparator": "RANGE", "approximate": "on", "measurement_role": "HISTORICAL", "original_unit": ""})
    assert form.is_valid(), form.errors
    content = field_content("lesion.suvmax", form.cleaned_data["value"], form.cleaned_data["raw_value"])
    assert "历史记录值" in content["text"]


def test_larger_marker_source_ends_with_its_measurement_without_other_findings(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "双肺多发结节，较大者位于左肺上叶，约12mm，OTHER_DESCRIPTION_CANARY。")
    field = fields(document, "lesion.maximum_scope")[0]
    assert field.raw_text == "双肺多发结节，较大者位于左肺上叶，约12mm"
    assert field.source_fragments.get().raw_text == field.raw_text


def test_complete_comparison_line_does_not_swallow_following_unnumbered_impressions(django_user_model):
    _, _, document, _, _ = imaging(django_user_model, "左肺结节约12mm。",
        impression="左肺结节大小较前无明显变化\n右肾囊肿\nFOLLOWING_IMPRESSION_CANARY")
    statements = fields(document, "comparison.statement")
    assert len(statements) == 1
    assert statements[0].automatic_content["value"]["text"] == "左肺结节大小较前无明显变化"
    assert "FOLLOWING_IMPRESSION_CANARY" not in statements[0].raw_text


def test_wrapped_incomplete_comparison_and_continuation_keep_the_complete_assertion(django_user_model):
    _, _, document, _, _ = imaging(django_user_model, "左肺结节约12mm。",
        impression="左肺结节较前稍\n缩小，\n伴周围条索影同前。")
    statement = fields(document, "comparison.statement")[0]
    assert "缩小" in statement.raw_text and "伴周围条索影同前" in statement.raw_text


@pytest.mark.parametrize("body", [
    "左肾上腺未见增粗，SUVmax2.1。",
    "左肾上腺无异常摄取，SUVmax2.1。",
])
def test_negated_local_observation_is_not_created_for_an_organ_uptake(django_user_model, body):
    _, _, document, _, _ = imaging(django_user_model, body)
    assert not fields(document, "lesion.suvmax")
    assert not document.facts.filter(entity_key__startswith="lesion:uptake-").exists()


def test_negation_of_a_different_finding_does_not_remove_explicit_local_abnormality(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "左肾上腺未见结节，但局部轻度增粗，SUVmax2.1。")
    suv = fields(document, "lesion.suvmax")[0]
    assert suv.automatic_content["value"]["values"] == ["2.1"]
    assert "未见结节" in suv.raw_text and "局部轻度增粗" in suv.raw_text


@pytest.mark.parametrize("marker", ["既往检查", "上次的", "前次检查"])
def test_explicit_prior_examination_is_not_labelled_as_current(django_user_model, marker):
    _, _, document, _, _ = imaging(django_user_model,
        f"左肺上叶结节约12mm，{marker}SUVmax5.4，本次SUVmax3.2。")
    suvs = fields(document, "lesion.suvmax")
    assert [f.automatic_content["value"]["measurement_role"] for f in suvs] == ["HISTORICAL", "CURRENT"]
    assert [f.automatic_content["value"]["values"] for f in suvs] == [["5.4"], ["3.2"]]


@pytest.mark.parametrize("description", ["未见增粗及异常摄取", "无增粗或异常摄取", "未见增粗、增厚及异常摄取"])
def test_coordinated_negative_findings_do_not_create_a_local_abnormality(django_user_model, description):
    _, _, document, _, _ = imaging(django_user_model, f"左肾上腺{description}，SUVmax2.1。")
    assert not fields(document, "lesion.suvmax")
    assert not document.facts.filter(entity_key__startswith="lesion:uptake-").exists()


@pytest.mark.parametrize("separator", ["，但", "；", "。"])
def test_coordinated_negation_ends_before_an_explicit_separate_positive_observation(django_user_model, separator):
    _, _, document, _, _ = imaging(django_user_model,
        f"左肾上腺未见增粗及异常摄取{separator}左肾上腺局部轻度增厚，SUVmax2.1。")
    suv = fields(document, "lesion.suvmax")[0]
    assert suv.automatic_content["value"]["values"] == ["2.1"]
    assert "局部轻度增厚" in suv.raw_text


def test_explicit_unknown_measurement_time_is_not_current_by_default(django_user_model):
    _, _, document, _, _ = imaging(django_user_model,
        "左肺上叶结节约12mm，检查时间不详SUVmax5.4，本次SUVmax3.2。")
    assert [f.automatic_content["value"]["measurement_role"] for f in fields(document, "lesion.suvmax")] == ["UNKNOWN", "CURRENT"]
