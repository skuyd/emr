"""Synthetic persisted pathology sources; no private gold or real OCR is read."""
from copy import deepcopy

import pytest

from tools.pathology_source_mapping import MappingInputError, map_document, original_pages
from tests.facts.test_pathology_extraction import report_rows
from tests.facts.test_pathology_pipeline import fixture


pytestmark = pytest.mark.django_db
OCR = "b" * 64


def panel(django_user_model, *, name="mapping", rows=None):
    rows = report_rows() if rows is None else rows
    _, _, document, version, _ = fixture(django_user_model, name=name, rows=rows)
    fixed = [{"page_number": 1, "width": 1000, "height": 1500,
              "regions": [{"text": row.text, "reading_order": i, "polygon": deepcopy(row.polygon),
                           "confidence": .98} for i, row in enumerate(rows)]}]
    return document, version, fixed


def mapped(document, fixed, **kwargs):
    return map_document(document.pk, fixed, source_sha256=document.sha256, ocr_sha256=OCR,
                        execution_status=kwargs.pop("execution_status", {1: "COMPLETE"}), **kwargs)


def scores(result):
    return [item for page in result["pages"] for item in page["items"] if item["field_key"] == "ihc.score"]


def test_actual_fields_keep_independent_scores_and_original_bindings(django_user_model):
    document, version, fixed = panel(django_user_model)
    original = deepcopy(fixed)
    result = mapped(document, fixed)
    actual = scores(result)
    assert len(actual) == 2
    assert [(item["value"]["score_kind"], item["value"]["values"], item["value"]["unit"]) for item in actual] == [
        ("TPS", ["13"], "%"), ("CPS", ["21"], None)]
    assert result["receipt"]["candidate_count"] == document.facts.filter(representation="FIELD", category="PATHOLOGY").count()
    for item in actual:
        assert item["mapping_diagnostics"] == []
        assert item["bindings"]["MARKER"]["target"]["value"]["code"] == "PD_L1"
        assert item["bindings"]["SPECIMEN"]["target"]["value"]["raw"] == "SYN-A"
        for proof in item["value_evidence"] + item["bindings"]["MARKER"]["proof_evidence"]:
            raw = fixed[0]["regions"][proof["region_index"]]
            assert proof["raw_text"] == raw["text"][proof["start_offset"]:proof["end_offset"]]
            assert proof["polygon"] == raw["polygon"] and proof["reading_order"] == raw["reading_order"]
    assert fixed == original


def test_original_array_order_and_outer_whitespace_are_not_runtime_offsets(django_user_model):
    document, version, fixed = panel(django_user_model)
    fixed[0]["regions"][4]["text"] = " \t" + fixed[0]["regions"][4]["text"] + "  "
    fixed[0]["regions"].reverse()
    item = scores(mapped(document, fixed))[0]
    proof = item["value_evidence"][0]
    assert proof["reading_order"] == 4 and proof["region_index"] == 2
    block = fixed[0]["regions"][2]
    assert proof["start_offset"] == block["text"].index("TPS")
    assert proof["raw_text"] == "TPS：13%"
    assert item["mapping_diagnostics"] == []


def test_mapper_does_not_borrow_a_label_from_unpersisted_surroundings(django_user_model):
    document, _, fixed = panel(django_user_model)
    identity = next(i for i in mapped(document, fixed)["pages"][0]["items"] if i["field_key"] == "specimen.identity")
    assert identity["value_evidence"] == identity["label_evidence"]
    assert "标本编号" not in "".join(p["raw_text"] for p in identity["label_evidence"])


@pytest.mark.parametrize("change", ["text", "polygon", "reading_order", "fragment_text", "fragment_offset", "evidence_text"])
def test_changed_persisted_proof_keeps_candidate_with_error_instead_of_trusting_self_report(django_user_model, change):
    from apps.facts.models import FactSourceFragment
    from apps.processing.models import OcrBlock, SourceEvidence
    document, _, fixed = panel(django_user_model)
    fact = document.facts.filter(field_key="ihc.score").order_by("reading_order").first()
    fragment = fact.source_fragments.first()
    if change == "text":
        OcrBlock.objects.filter(pk=fragment.ocr_block_id).update(text=fragment.ocr_block.text.replace("13", "14"))
    elif change == "polygon":
        OcrBlock.objects.filter(pk=fragment.ocr_block_id).update(polygon=[[.1, .1], [.2, .1], [.2, .2], [.1, .2]])
    elif change == "reading_order":
        OcrBlock.objects.filter(pk=fragment.ocr_block_id).update(reading_order=999)
    elif change == "fragment_text":
        FactSourceFragment.objects.filter(pk=fragment.pk).update(raw_text="TPS：14%")
    elif change == "fragment_offset":
        FactSourceFragment.objects.filter(pk=fragment.pk).update(start_offset=0)
    else:
        SourceEvidence.objects.filter(pk=fragment.evidence_id).update(source_text="TPS：14%")
    item = next(i for i in scores(mapped(document, fixed)) if i["candidate_id"] == str(fact.pk))
    assert item["value"]["values"] == ["13"]
    assert item["mapping_diagnostics"] and not item["value_evidence"]


@pytest.mark.parametrize("change", ["cycle", "other_report", "missing"])
def test_context_target_is_reloaded_and_missing_wrong_report_or_cycle_is_not_synthesized(django_user_model, change):
    import uuid
    from apps.facts.models import Fact
    document, _, fixed = panel(django_user_model)
    fact = document.facts.filter(field_key="ihc.score").first()
    # Caller caches do not supply targets to the mapper.
    stale = deepcopy(fact.automatic_content)
    content = deepcopy(stale)
    target = str(fact.pk)
    if change == "other_report":
        other, _, _ = panel(django_user_model, name="other-mapping")
        target = str(other.facts.filter(field_key="ihc.marker").get().pk)
    elif change == "missing":
        target = str(uuid.uuid4())
    content["entity_context"]["bindings"][-1]["target_fact_id"] = target
    Fact.objects.filter(pk=fact.pk).update(automatic_content=content)
    item = next(i for i in scores(mapped(document, fixed)) if i["candidate_id"] == str(fact.pk))
    assert item["mapping_diagnostics"]
    assert item["bindings"]["MARKER"]["target"] is None
    assert stale != Fact.objects.get(pk=fact.pk).automatic_content


def test_fullwidth_source_characters_are_not_replaced_by_the_parser_matching_view(django_user_model):
    rows = report_rows()
    rows[4].text = "检测结果：PD-L1 TPS：１３％ CPS：２１"
    document, _, fixed = panel(django_user_model, rows=rows)
    item = scores(mapped(document, fixed))[0]
    assert item["value"]["values"] == ["13"] and item["value"]["unit"] == "%"
    assert item["value_evidence"][0]["raw_text"] == "TPS：１３％"
    assert item["production_identity"]["raw_value"] == "TPS：１３％"


def test_malformed_binding_does_not_erase_an_actual_candidate_or_claim_page_success(django_user_model):
    from apps.facts.models import Fact
    document, _, fixed = panel(django_user_model)
    fact = document.facts.filter(field_key="ihc.score").first()
    content = deepcopy(fact.automatic_content)
    content["entity_context"]["bindings"][-1]["target_fact_id"] = {"invalid": "identifier"}
    Fact.objects.filter(pk=fact.pk).update(automatic_content=content)
    result = mapped(document, fixed)
    assert len(scores(result)) == 2 and result["pages"][0]["status"] == "FAILED"
    item = next(i for i in scores(result) if i["candidate_id"] == str(fact.pk))
    assert item["value"]["values"] == fact.automatic_content["value"]["values"]
    assert item["mapping_diagnostics"][0]["reason"] == "projection_exception"


def test_unknown_binding_remains_unknown_and_duplicate_scores_are_not_collapsed(django_user_model):
    from tests.facts.test_clinical_segments import block
    rows = [block("病理诊断报告书\n检测结果：PD-L1 TPS：13% TPS：13%")]
    document, _, fixed = panel(django_user_model, rows=rows)
    actual = scores(mapped(document, fixed))
    assert len(actual) == 2
    assert all(i["bindings"]["ASSAY"]["state"] == "UNKNOWN" and i["bindings"]["ASSAY"]["target"] is None for i in actual)
    assert actual[0]["value_evidence"][0]["start_offset"] != actual[1]["value_evidence"][0]["start_offset"]


@pytest.mark.parametrize("status", ["FAILED", "NOT_RUN"])
def test_page_failure_or_not_run_never_becomes_complete_because_it_has_candidates(django_user_model, status):
    document, _, fixed = panel(django_user_model)
    result = mapped(document, fixed, execution_status={1: status})
    assert result["pages"][0]["status"] == status and scores(result)


def test_clinical_extraction_failure_is_not_a_successful_empty_page(django_user_model):
    from apps.facts.models import ClinicalExtraction
    document, version, fixed = panel(django_user_model)
    ClinicalExtraction.objects.filter(parsing_version=version).update(status="FAILED")
    result = mapped(document, fixed)
    assert result["pages"][0]["status"] == "FAILED" and scores(result)
    assert result["receipt"]["clinical_status"] == "FAILED"


def test_foreign_source_or_bad_execution_envelope_is_an_input_error(django_user_model):
    document, _, fixed = panel(django_user_model)
    with pytest.raises(MappingInputError):
        map_document(document.pk, fixed, source_sha256="f" * 64, ocr_sha256=OCR, execution_status={1: "COMPLETE"})
    with pytest.raises(MappingInputError):
        mapped(document, fixed, execution_status={2: "COMPLETE"})


def test_original_index_uses_source_transform_without_mutating_cached_polygons():
    fixed = [{"page_number": 1, "source_transform": [[.5, 0, .2], [0, .5, .1], [0, 0, 1]],
              "regions": [{"text": "合成", "reading_order": 7, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}]
    before = deepcopy(fixed)
    original = original_pages(fixed, source_sha256="a" * 64, ocr_sha256=OCR)[0]
    assert original["regions"][0]["polygon"] == [[.2, .1], [.7, .1], [.7, .6], [.2, .6]]
    assert fixed == before
