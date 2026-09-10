from copy import deepcopy

import pytest

from tests.facts.pathology_factories import score_value


def form(key, data, *, value=None):
    from apps.facts.pathology_forms import PathologyValueForm

    return PathologyValueForm(key, data, value=value)


def test_tps_and_cps_forms_keep_unit_absence_and_explicit_comparator():
    values = {"raw_value": "CPS ≤21", "score_kind": "CPS", "scalar_1": "21", "scalar_2": "", "comparator": "LE",
              "original_unit": "", "assertion": "AS_REPORTED_NO_POSITIVITY_INFERRED", "checked_original": "on"}
    actual = form("ihc.score", values)
    assert actual.is_valid(), actual.errors
    result = actual.cleaned_data["value"]
    assert result["unit"] is None and result["unit_state"] == "NOT_PRINTED"
    assert result["score_kind"] == "CPS" and result["scale_kind"] == "SCORE" and result["comparator"] == "LE"
    assert result["assertion"] == "AS_REPORTED_NO_POSITIVITY_INFERRED"
    values.update(score_kind="TPS", original_unit="%", comparator="RANGE", scalar_1="11", scalar_2="13", approximate="on")
    actual = form("ihc.score", values)
    assert actual.is_valid(), actual.errors
    assert actual.cleaned_data["value"]["values"] == ["11", "13"]
    assert actual.cleaned_data["value"]["approximate"] and actual.cleaned_data["value"]["scale_kind"] == "PROPORTION"


@pytest.mark.parametrize("kind", ["specimen.histology", "specimen.margin", "pathology.reported_stage", "ihc.result"])
def test_source_reported_text_and_assertion_are_independent_visible_controls(kind):
    actual = form(kind, {"raw_value": "可疑原文", "text_value": "可疑原文", "assertion": "UNCERTAIN"})
    assert actual.is_valid(), actual.errors
    assert actual.cleaned_data["value"] == {"text": "可疑原文", "assertion": "UNCERTAIN"}


def test_ihc_method_and_explicit_date_roles_are_supported_without_default_guess():
    actual = form("assay.method", {"raw_value": "免疫组化", "code": "IHC", "coded_raw": "免疫组化"})
    assert actual.is_valid(), actual.errors
    for kind in ("collection_date", "received_date", "report_date"):
        date = form("assay." + kind, {"raw_value": "日期未注明", "date_value": "", "precision": "UNKNOWN"})
        assert date.is_valid(), date.errors
        assert date.cleaned_data["value"] == {"value": None, "precision": "UNKNOWN"}


def test_identity_forms_preserve_literal_label_and_unknown_marker_code():
    identity = form("specimen.identity", {"raw_value": "标本甲", "identity_label": "标本甲", "identity_raw": "标本甲"})
    assert identity.is_valid(), identity.errors
    marker = form("ihc.marker", {"raw_value": "原文标记", "identity_label": "原文标记", "identity_raw": "原文标记", "marker_code": ""})
    assert marker.is_valid(), marker.errors
    assert marker.cleaned_data["value"]["code"] is None


def test_existing_score_form_round_trip_does_not_invent_original_units():
    from apps.facts.pathology_forms import PathologyValueForm

    original = score_value("CPS", "21", None)
    ui = PathologyValueForm("ihc.score", value=original, initial={"raw_value": original["raw"]})
    data = {key: value for key, value in ui.initial.items() if value is not False}
    submitted = form("ihc.score", data, value=original)
    assert submitted.is_valid(), submitted.errors
    assert submitted.cleaned_data["value"] == original


def test_node_group_controls_keep_missing_count_null_and_order():
    data = {"raw_value": "组甲送检5；组乙阳性2", "node_count": "2", "assertion": "SOURCE_TEXT_ONLY_NOT_DIAGNOSED",
            "group_1_label": "组甲", "group_1_sampled": "5", "group_1_positive": "", "group_1_raw": "组甲送检5",
            "group_2_label": "组乙", "group_2_sampled": "", "group_2_positive": "2", "group_2_raw": "组乙阳性2"}
    actual = form("specimen.nodes", data)
    assert actual.is_valid(), actual.errors
    assert [(group["sampled"], group["positive"]) for group in actual.cleaned_data["value"]["groups"]] == [("5", None), (None, "2")]
    invalid = deepcopy(data)
    invalid["node_count"] = "1000000"
    assert not form("specimen.nodes", invalid).is_valid()


def test_pathology_dimensions_can_preserve_absent_units_without_inventing_measurement_object():
    data = {"raw_value": "尺寸约3×2，原文未注明单位", "size_1": "3", "unit_1": "", "size_2": "2", "unit_2": "",
            "measurement_object": "UNKNOWN", "measurement_role": "UNKNOWN", "approximate": "on"}
    actual = form("specimen.dimensions", data)
    assert actual.is_valid(), actual.errors
    value = actual.cleaned_data["value"]
    assert value["components"] == [{"value": "3", "unit": None, "axis": None}, {"value": "2", "unit": None, "axis": None}]
    assert value["measurement_object"] == "UNKNOWN" and value["measurement_role"] == "UNKNOWN"
    from apps.facts.clinical_schema import validate_value
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        validate_value("lesion.dimensions", {key: item for key, item in value.items() if key != "measurement_object"})
    for number in ("NaN", "Infinity", "-1", "1e20"):
        assert not form("specimen.dimensions", {**data, "size_1": number}).is_valid()
