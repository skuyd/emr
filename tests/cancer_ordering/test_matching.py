from copy import deepcopy
from decimal import Decimal

import pytest

from apps.cancer_ordering.matching import eligible_for_auto, literal_candidates


@pytest.mark.parametrize(("raw", "category", "label", "profile"), [
    ("出院诊断：肺癌。", "DIAGNOSIS", "肺癌", "LUNG"),
    ("病理诊断：右肺上叶浸润性腺癌 pT2aN1M0 ⅢA期。", "PATHOLOGY", "右肺上叶浸润性腺癌", "LUNG"),
    ("病理结论：胰头导管腺癌。", "PATHOLOGY", "胰头导管腺癌", "PANCREAS"),
    ("主要诊断：胰腺癌。", "DIAGNOSIS", "胰腺癌", "PANCREAS"),
    ("临床诊断：胰头癌。", "DIAGNOSIS", "胰头癌", "PANCREAS"),
    ("【病理诊断】：右肺上叶浸润性\n腺癌。", "PATHOLOGY", "右肺上叶浸润性腺癌", "LUNG"),
])
def test_only_complete_reported_literals_receive_a_display_profile(raw, category, label, profile):
    rows = literal_candidates(raw, category)
    assert len(rows) == 1
    row = rows[0]
    assert (row["label"], row["profile"], row["assertion"], row["subject"]) == (
        label, profile, "AFFIRMED", "CURRENT_PRIMARY",
    )
    assert raw[row["start"]:row["end"]] == row["raw"]
    assert raw[row["match_start"]:row["match_end"]] == row["label_raw"]
    assert row["label_raw"].replace("\n", "") == label


@pytest.mark.parametrize(("body", "assertion", "subject"), [
    ("未见肺癌", "NEGATED", "CURRENT_PRIMARY"),
    ("不能除外肺癌", "UNCERTAIN", "CURRENT_PRIMARY"),
    ("无法排除肺癌", "UNCERTAIN", "CURRENT_PRIMARY"),
    ("肺癌？", "UNCERTAIN", "CURRENT_PRIMARY"),
    ("肺癌待排", "UNCERTAIN", "CURRENT_PRIMARY"),
    ("既往肺癌", "AFFIRMED", "HISTORICAL"),
    ("父亲患肺癌", "AFFIRMED", "OTHER_PERSON"),
])
def test_reported_negation_uncertainty_and_subject_are_not_erased(body, assertion, subject):
    rows = literal_candidates("临床诊断：" + body + "。", "DIAGNOSIS")
    assert len(rows) == 1
    assert (rows[0]["profile"], rows[0]["assertion"], rows[0]["subject"]) == ("LUNG", assertion, subject)
    assert not eligible_for_auto(rows[0], (Decimal("1"),), source_valid=True, reviewed=True)


@pytest.mark.parametrize("body", ["小细胞肺癌", "肺癌样病变", "肝癌", "胰头囊腺癌", "腺癌"])
def test_unsupported_complete_diagnoses_are_retained_without_substring_mapping(body):
    rows = literal_candidates("病理诊断：" + body + "。", "PATHOLOGY")
    assert len(rows) == 1 and rows[0]["profile"] is None
    assert body in rows[0]["raw"]


@pytest.mark.parametrize(("raw", "category"), [
    ("影像表现：肺结节，考虑肺癌。", "IMAGING"),
    ("送检目的：排除肺癌。", "DIAGNOSIS"),
    ("标本：右肺。IHC：TTF1（+）。", "PATHOLOGY"),
    ("病理诊断：肺组织，IHC阳性。", "PATHOLOGY"),
])
def test_category_organ_and_marker_are_not_proof_of_a_reported_diagnosis(raw, category):
    assert literal_candidates(raw, category) == ()


def test_conflicting_diagnoses_keep_both_source_occurrences():
    raw = "出院诊断：肺癌；胰腺癌。"
    rows = literal_candidates(raw, "DIAGNOSIS")
    assert [(row["label"], row["profile"]) for row in rows] == [("肺癌", "LUNG"), ("胰腺癌", "PANCREAS")]
    assert all(raw[row["start"]:row["end"]] == row["raw"] for row in rows)


def test_local_comma_and_conjunction_scopes_preserve_the_reported_assertions():
    rows = literal_candidates("临床诊断：未见肺癌，胰腺癌明确。", "DIAGNOSIS")
    assert [(row["label"], row["assertion"]) for row in rows] == [("肺癌", "NEGATED"), ("胰腺癌", "AFFIRMED")]
    coordinated = literal_candidates("临床诊断：未见肺癌及胰腺癌。", "DIAGNOSIS")
    assert [row["assertion"] for row in coordinated] == ["NEGATED", "NEGATED"]
    disjunction = literal_candidates("临床诊断：肺癌或胰腺癌。", "DIAGNOSIS")
    assert [row["assertion"] for row in disjunction] == ["UNCERTAIN", "UNCERTAIN"]


def test_metastatic_site_does_not_become_the_primary_cancer_profile():
    rows = literal_candidates("病理诊断：肺转移癌（原发待定）。", "PATHOLOGY")
    assert len(rows) == 1 and rows[0]["profile"] is None
    assert rows[0]["subject"] == "METASTATIC_SITE"


def test_an_unknown_diagnosis_beside_a_supported_one_is_not_dropped():
    rows = literal_candidates("临床诊断：肺癌合并肝癌。", "DIAGNOSIS")
    assert len(rows) == 2
    assert sum(row["profile"] == "LUNG" for row in rows) == 1
    assert sum(row["profile"] is None for row in rows) == 1


def test_original_unicode_offsets_and_duplicate_occurrences_remain_distinct():
    raw = "病理诊断：Ⅰ、肺癌；２．胰腺癌；肺癌。"
    rows = literal_candidates(raw, "PATHOLOGY")
    assert [row["profile"] for row in rows] == ["LUNG", "PANCREAS", "LUNG"]
    assert [row["match_start"] for row in rows] == [raw.index("肺癌"), raw.index("胰腺癌"), raw.rindex("肺癌")]
    assert len({(row["match_start"], row["match_end"]) for row in rows}) == 3


@pytest.mark.parametrize(("confidence", "expected"), [
    ((Decimal("0.95"),), True),
    ((Decimal("0.9499"),), False),
    ((Decimal("0.99"), Decimal("0.94")), False),
    ((Decimal("0.99"), None), False),
    ((), False),
])
def test_automatic_adoption_requires_every_actual_source_fragment_to_be_reliable(confidence, expected):
    row = {"profile": "LUNG", "assertion": "AFFIRMED", "subject": "CURRENT_PRIMARY"}
    assert eligible_for_auto(row, confidence, source_valid=True) is expected
    assert not eligible_for_auto(row, confidence, source_valid=False)


def test_manual_check_only_bypasses_ocr_confidence_and_never_source_or_assertion_rules():
    row = {"profile": "LUNG", "assertion": "AFFIRMED", "subject": "CURRENT_PRIMARY"}
    original = deepcopy(row)
    assert eligible_for_auto(row, (), source_valid=True, reviewed=True)
    assert not eligible_for_auto(row, (), source_valid=False, reviewed=True)
    assert not eligible_for_auto({**row, "assertion": "UNKNOWN"}, (), source_valid=True, reviewed=True)
    assert not eligible_for_auto({**row, "profile": None}, (), source_valid=True, reviewed=True)
    assert row == original


@pytest.mark.parametrize(("body", "assertion"), [
    ("未患肺癌", "NEGATED"), ("从未患肺癌", "NEGATED"), ("未罹患肺癌", "UNKNOWN"),
])
def test_an_unrecognized_negative_prefix_never_exposes_an_affirmative_inner_word(body, assertion):
    rows = literal_candidates("临床诊断：" + body + "。", "DIAGNOSIS")
    assert len(rows) == 1 and rows[0]["assertion"] == assertion
    assert not eligible_for_auto(rows[0], (Decimal(".99"),), source_valid=True)


def test_negation_of_a_following_finding_does_not_negate_the_reported_diagnosis():
    rows = literal_candidates("出院诊断：肺癌，未见淋巴结转移。", "DIAGNOSIS")
    assert len(rows) == 1
    assert (rows[0]["profile"], rows[0]["assertion"]) == ("LUNG", "AFFIRMED")


def test_plain_comma_lists_keep_both_diagnoses_and_unclear_negation_scope():
    rows = literal_candidates("出院诊断：肺癌，胰腺癌。", "DIAGNOSIS")
    assert [(row["profile"], row["assertion"]) for row in rows] == [("LUNG", "AFFIRMED"), ("PANCREAS", "AFFIRMED")]
    ambiguous = literal_candidates("出院诊断：未见肺癌，胰腺癌。", "DIAGNOSIS")
    assert [(row["profile"], row["assertion"]) for row in ambiguous] == [("LUNG", "NEGATED"), ("PANCREAS", "UNKNOWN")]


def test_one_uncertain_sibling_does_not_relabel_the_earlier_affirmed_diagnosis():
    rows = literal_candidates("临床诊断：肺癌及疑似胰腺癌。", "DIAGNOSIS")
    assert [(row["profile"], row["assertion"]) for row in rows] == [("LUNG", "AFFIRMED"), ("PANCREAS", "UNCERTAIN")]


def test_separate_heading_line_is_supported_but_a_longer_word_is_not_a_heading():
    rows = literal_candidates("病理诊断\n肺癌。", "PATHOLOGY")
    assert len(rows) == 1 and rows[0]["profile"] == "LUNG"
    assert literal_candidates("诊断性肺癌说明", "DIAGNOSIS") == ()
