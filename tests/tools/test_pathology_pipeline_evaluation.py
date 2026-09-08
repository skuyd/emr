"""Synthetic frozen OCR through real application persistence, mapper and scorer."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tools.pathology_pipeline_evaluation import EvaluationInputError, predict_sources, verify_manifest, verify_coverage, checked_output
from tools.pathology_molecular_evaluation import evaluate
from tests.facts.test_pathology_extraction import report_rows


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixed_manifest(tmp_path):
    sources = []
    for number in (1, 2):
        original = tmp_path / f"synthetic-pathology-{number}.bin"
        original.write_bytes(f"synthetic-pathology-original-{number}".encode())
        identity = digest(original)
        rows = report_rows() if number == 1 else []
        page = {"page_number": 1, "width": 1000, "height": 1500, "provider": "synthetic", "provider_version": "1",
                "provider_metadata": {}, "regions": [{"text": row.text, "polygon": deepcopy(row.polygon),
                    "reading_order": i, "confidence": .98} for i, row in enumerate(rows)]}
        pages = [page]
        if number == 1:
            pages.append({**deepcopy(page), "page_number": 2, "regions": []})
        cache = tmp_path / (identity + ".json")
        cache.write_text(json.dumps({"source_file_hash": identity, "pages": pages}, ensure_ascii=False), encoding="utf-8")
        sources.append({"source_number": number, "source_path": str(original), "source_sha256": identity,
                        "ocr_path": str(cache), "ocr_sha256": digest(cache), "ocr_pages": len(pages)})
    return {"sources": sources}


def synthetic_gold(manifest):
    source = manifest["sources"][0]
    text = report_rows()[1].text
    identity = {"source_sha256": source["source_sha256"], "ocr_sha256": source["ocr_sha256"], "page": 1}
    def proof(start, end):
        return {**identity, "region_index": 1, "reading_order": 1, "start_offset": start, "end_offset": end,
                "raw_text": text[start:end], "polygon": deepcopy(report_rows()[1].polygon)}
    start = text.index("SYN-A")
    field = {**identity, "gold_id": "SYN-IDENTITY", "field_key": "specimen.identity",
             "expected_value": {"raw": "SYN-A", "label": "SYN-A"}, "source_role": "PRIMARY_ASSAY_METADATA",
             "value_evidence": [proof(start, start + 5)], "label_evidence": [proof(0, start)], "bindings": {}}
    coverage = [{"source_sha256": s["source_sha256"], "ocr_sha256": s["ocr_sha256"], "page": page,
                 "state": "SCOPED_FIELDS_AND_EXCLUSIONS" if s == source and page == 1 else "UNREVIEWED_UNJUDGED"}
                for s in manifest["sources"] for page in range(1, s["ocr_pages"] + 1)]
    return {"fields": [field], "negative_boundaries": [], "coverage": coverage}, {"rules": []}


@pytest.mark.django_db(transaction=True)
def test_actual_pipeline_mapping_and_scoring_preserve_unjudged_pages_and_missing_label_proof(tmp_path):
    from apps.labs.dictionary import phase_two_dictionary
    from apps.documents.models import Document
    manifest = fixed_manifest(tmp_path)
    gold, rules = synthetic_gold(manifest)  # authored before the application runs
    before = deepcopy(manifest)
    predictions, originals, execution = predict_sources(manifest, phase_two_dictionary())
    assert len(predictions["pages"]) == len(originals["pages"]) == 3
    assert [p["status"] for p in predictions["pages"]] == ["COMPLETE"] * 3
    assert Document.objects.count() == execution["fixed_source_count"] == 2
    assert manifest == before
    items = predictions["pages"][0]["items"]
    assert [i["value"]["score_kind"] for i in items if i["field_key"] == "ihc.score"] == ["TPS", "CPS"]
    result = evaluate(gold, predictions, originals, rules)
    assert result["summary"]["candidate_count"] == len(items)
    assert result["summary"]["scope"]["unreviewed_pages"] == 2
    assignment = result["assignments"][0]
    assert assignment["status"] == "SOURCE_UNVERIFIED"
    assert assignment["components"]["value"]["status"] == "CORRECT"
    assert assignment["components"]["own_original_source_proof"]["status"] == "SOURCE_UNVERIFIED"
    assert execution["clinical_failed_sources"] == execution["failed_pipeline_sources"] == 0
    assert predictions["pages"][1]["items"] == predictions["pages"][2]["items"] == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("stage", ["persistence", "clinical"])
def test_real_pipeline_and_independent_clinical_savepoint_failures_remain_failed_pages(tmp_path, monkeypatch, stage):
    from apps.labs.dictionary import phase_two_dictionary
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.facts import clinical_extraction
    manifest = fixed_manifest(tmp_path)
    if stage == "persistence":
        original = DocumentProcessingPipeline._persist
        def fail(self, context, document, *args, **kwargs):
            if document.sha256 == manifest["sources"][0]["source_sha256"]:
                raise RuntimeError("synthetic-persistence-failure")
            return original(self, context, document, *args, **kwargs)
        monkeypatch.setattr(DocumentProcessingPipeline, "_persist", fail)
    else:
        original = clinical_extraction.extract_clinical_version
        def fail(version, **kwargs):
            if version.document.sha256 == manifest["sources"][0]["source_sha256"]:
                raise RuntimeError("synthetic-clinical-failure")
            return original(version, **kwargs)
        monkeypatch.setattr(clinical_extraction, "extract_clinical_version", fail)
    predictions, originals, execution = predict_sources(manifest, phase_two_dictionary())
    assert [p["status"] for p in predictions["pages"]] == ["FAILED", "FAILED", "COMPLETE"]
    assert len(originals["pages"]) == 3
    assert execution["failed_pipeline_sources"] == int(stage == "persistence")
    assert execution["clinical_failed_sources"] == int(stage == "clinical")
    gold, rules = synthetic_gold(manifest)
    assert evaluate(gold, predictions, originals, rules)["assignments"][0]["status"] == "MISSING"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("which", ["source_path", "ocr_path"])
def test_all_input_files_are_verified_before_first_database_write(tmp_path, which):
    from apps.labs.dictionary import phase_two_dictionary
    from apps.documents.models import Document
    manifest = fixed_manifest(tmp_path)
    with Path(manifest["sources"][-1][which]).open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(EvaluationInputError):
        predict_sources(manifest, phase_two_dictionary())
    assert not Document.objects.exists()


@pytest.mark.parametrize("change", ["source", "number", "page", "cache_source", "region_order", "counts"])
def test_manifest_cannot_narrow_or_substitute_any_original_inventory(tmp_path, change):
    manifest = fixed_manifest(tmp_path)
    source = manifest["sources"][0]
    if change == "source":
        manifest["sources"].append(deepcopy(source))
    elif change == "number":
        manifest["sources"][1]["source_number"] = 1
    elif change == "page":
        source["ocr_pages"] += 1
    elif change in {"cache_source", "region_order"}:
        path = Path(source["ocr_path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        if change == "cache_source":
            payload["source_file_hash"] = "f" * 64
        else:
            payload["pages"][0]["regions"][1]["reading_order"] = 0
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        source["ocr_sha256"] = digest(path)
    with pytest.raises(EvaluationInputError):
        verify_manifest(manifest, expected_counts=(64, 124, 128) if change == "counts" else (2, 3, 4))


def test_source_coverage_is_checked_independently_of_predictions(tmp_path):
    manifest = fixed_manifest(tmp_path)
    gold, _ = synthetic_gold(manifest)
    verify_coverage(manifest, gold["coverage"])
    with pytest.raises(EvaluationInputError):
        verify_coverage(manifest, gold["coverage"][:-1])
    with pytest.raises(EvaluationInputError):
        verify_coverage(manifest, gold["coverage"] + gold["coverage"][:1])


def test_output_is_a_fresh_private_directory_and_never_replaces_old_evidence(tmp_path):
    root = tmp_path / "workspace"
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    selected = runtime / "new-run"
    assert checked_output(root, selected) == selected.resolve()
    selected.mkdir()
    with pytest.raises(EvaluationInputError):
        checked_output(root, selected)
    with pytest.raises(EvaluationInputError):
        checked_output(root, root / "docs" / "predictions")


def scoring_contract(manifest):
    from tools.pathology_molecular_evaluation import COMPONENTS, STATES
    gold, predicates = synthetic_gold(manifest)
    protocol = {"protocol_version": "PATHOLOGY_IHC_SCOPED_SCORING_V1", "field_components": list(COMPONENTS),
                "field_outcomes": dict.fromkeys(STATES, "synthetic"), "population": {
                    "inputs": 2, "pages": 3, "expected_fields": 1, "independent_score_fields": 0,
                    "context_and_metadata_fields": 1, "negative_regions": 0, "out_of_gold_pages": 2,
                    "unreviewed_subset": 2, "scope_visual_only_subset": 0}}
    contract = {"version": "PATHOLOGY_IHC_EVALUATION_CONTRACT_V1", "components": dict.fromkeys(COMPONENTS, "synthetic")}
    predicates.update(version="PATHOLOGY_IHC_NEGATIVE_PREDICATES_V1", gold_sha256="1" * 64, protocol_sha256="2" * 64)
    return gold, protocol, contract, predicates


@pytest.mark.parametrize("change", [None, "population", "components", "states", "gold_count", "gold_binding", "protocol_binding", "contract"])
def test_runner_cannot_bind_a_different_scoring_population_or_contract(tmp_path, change):
    from tools import pathology_pipeline_evaluation as runner
    manifest = fixed_manifest(tmp_path)
    gold, protocol, contract, predicates = scoring_contract(manifest)
    if change == "population":
        protocol["population"]["pages"] = 124
    elif change == "components":
        protocol["field_components"].pop()
    elif change == "states":
        del protocol["field_outcomes"]["SOURCE_UNVERIFIED"]
    elif change == "gold_count":
        protocol["population"]["expected_fields"] = 10
    elif change == "gold_binding":
        predicates["gold_sha256"] = "9" * 64
    elif change == "protocol_binding":
        predicates["protocol_sha256"] = "9" * 64
    elif change == "contract":
        contract["version"] = "different"
    kwargs = {"expected_counts": (2, 3, 4), "input_hashes": {"gold": "1" * 64, "protocol": "2" * 64}, "gold": gold}
    if change is None:
        runner.verify_scoring_contract(protocol, contract, predicates, **kwargs)
    else:
        with pytest.raises(EvaluationInputError):
            runner.verify_scoring_contract(protocol, contract, predicates, **kwargs)
