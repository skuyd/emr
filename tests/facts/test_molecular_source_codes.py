import pytest
from django.core.exceptions import ValidationError

from apps.facts.molecular_source_codes import assertion_codes, validate_assertion_code, validate_detection_code


@pytest.mark.parametrize(("code", "raw"), [
    ("POSITIVE", "阳性"), ("DETECTED", "本范围明确检出变异"), ("NEGATIVE", "阴性"),
    ("NOT_DETECTED", "未检出"), ("NOT_DETECTED", "没有检出"), ("NOT_DETECTED", "not detected"),
    ("NOT_DETECTED", "SYN no copy-number change in the tested scope"),
    ("UNCERTAIN", "原结果不确定"), ("NOT_TESTED", "未检测"), ("NOT_PROVIDED", "原件注明未提供"),
])
def test_finite_explicit_assertions_keep_their_original_meaning(code, raw):
    validate_assertion_code(code, raw)
    opposite = "NEGATIVE" if code in {"POSITIVE", "DETECTED"} else "POSITIVE"
    with pytest.raises(ValidationError):
        validate_assertion_code(opposite, raw)


@pytest.mark.parametrize("raw", ["not positive", "not  positive", "非阳性", "未呈阳性", "SYN uninterpreted words", "阴性；阳性", "not detected; detected"])
def test_unmapped_or_contradictory_words_do_not_become_reported_uncertainty_or_absence(raw):
    assert not assertion_codes(raw)
    for code in ("POSITIVE", "NEGATIVE", "UNCERTAIN", "NOT_PROVIDED"):
        with pytest.raises(ValidationError):
            validate_assertion_code(code, raw)


@pytest.mark.parametrize(("code", "raw"), [
    ("SMALL_VARIANT", "小变异"), ("COPY_NUMBER", "SYN copy-number"), ("FUSION", "融合"), ("MSI", "MSI"), ("TMB", "TMB"),
])
def test_finite_detection_kind_codes_require_the_printed_kind(code, raw):
    validate_detection_code({"code": code, "raw": raw})
    with pytest.raises(ValidationError):
        validate_detection_code({"code": "COPY_NUMBER" if code != "COPY_NUMBER" else "FUSION", "raw": raw})


def test_other_retains_a_literal_outside_the_supported_finite_kinds():
    validate_detection_code({"code": "OTHER", "raw": "SYN another stated test kind"})
    with pytest.raises(ValidationError):
        validate_detection_code({"code": "COPY_NUMBER", "raw": "SYN another stated test kind"})
