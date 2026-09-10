from copy import deepcopy
from itertools import combinations

import pytest

from tools.lesion_relation_evaluation import evaluate_relations, validate_execution_approval


def frozen_example(label="UNJUDGED_WHETHER_SAME_ENTITY"):
    reports, nodes, sources, observations = [], [], [], []
    for index, size in ((1, "12"), (2, "15")):
        quote = f"左肺见结节，长径{size}mm。"
        source = {"source_number": index, "source_sha256": str(index) * 64, "ocr_sha256": "a" * 64, "patient_group": "P01"}
        sources.append(source)
        field = {"field_key": "lesion.site", "entity_key": "lesion:01", "value": {"text": "左肺"},
                 "sources": [{"page": 1, "raw_quote": quote, "ocr_offsets": None, "exact_polygon": None}]}
        reports.append({"id": f"G{index}", **source, "page_ranges": [[1, 1]],
                        "original_context_fields": [], "comparison_fields": []})
        nodes.append({"id": f"G{index}/lesion:01", "report_id": f"G{index}", "source_number": index,
                      "patient_group": "P01", "local_identity_source": deepcopy(field), "original_fields": [deepcopy(field)]})
        actual = {"id": f"F{index}", "field_key": "lesion.site", "content": {"value": {"text": "左肺"}},
                  "source_valid": True, "status": "PENDING", "usable": False,
                  "fragments": [{"page": 1, "raw_text": quote, "start_offset": 0, "end_offset": len(quote), "polygon": []}]}
        observations.append({"id": f"O{index}", "report_id": f"R{index}", "source_number": index,
                             "patient_group": "P01", "report_pages": [1], "report_source_valid": True,
                             "fields": [actual], "context_fields": [], "status": "UNASSIGNED"})
    gold = {"reports": reports, "nodes": nodes,
            "pairs": [{"left_id": nodes[0]["id"], "right_id": nodes[1]["id"], "label": label}]}
    prediction = {"sources": sources, "observations": observations, "stable_lesions": [], "assignments": [],
                  "proposals": [{"id": "P1", "first_id": "O1", "second_id": "O2", "status": "PENDING",
                                 "reasons": [{"code": "explicit_location_equal", "field_ids": ["F1", "F2"]}]}]}
    return gold, prediction


def test_unjudged_pairs_are_review_work_not_invented_positive_accuracy():
    gold, prediction = frozen_example()
    score, audit = evaluate_relations(gold, prediction)
    assert score["observations"] == {"gold": 2, "predicted": 2, "mapped": 2, "missing": 0, "unmapped": 0, "outside_scope": 0}
    assert score["proposals"]["by_label"] == {"UNJUDGED_WHETHER_SAME_ENTITY": 1}
    assert score["identity_candidate_metrics"] == {"precision": None, "recall": None, "f1": None, "positive_gold": 0}
    assert score["reason_sources"] == {"verified": 1, "unverified": 0}
    assert score["automatic_confirmation_failures"] == 0
    assert audit["proposals"][0]["source_verified"]


@pytest.mark.parametrize("bad_source", ["右肾见囊肿，长径12mm。", "左肺", "12mm", "左肺见结节，长径12mm"])
def test_same_page_wrong_clause_organ_or_number_alone_cannot_map_a_local_observation(bad_source):
    gold, prediction = frozen_example()
    prediction["observations"][0]["fields"][0]["fragments"][0]["raw_text"] = bad_source
    score, audit = evaluate_relations(gold, prediction)
    assert score["observations"]["mapped"] == 1 and score["observations"]["missing"] == 1
    assert score["observations"]["unmapped"] == 1
    assert score["proposals"]["by_label"] == {"UNMAPPED_ENDPOINT": 1}
    assert score["reason_sources"] == {"verified": 0, "unverified": 1}


def test_duplicate_prediction_mapping_is_not_resolved_greedily_using_relation_labels():
    gold, prediction = frozen_example("SAME_ENTITY_EXPLICIT")
    duplicate = deepcopy(prediction["observations"][0])
    duplicate["id"] = "O3"
    duplicate["fields"][0]["id"] = "F3"
    prediction["observations"].append(duplicate)
    score, audit = evaluate_relations(gold, prediction)
    assert score["observations"]["mapped"] == 1 and score["observations"]["unmapped"] == 2
    assert score["observations"]["missing"] == 1
    assert {row["status"] for row in audit["mappings"] if row["observation_id"] in {"O1", "O3"}} == {"AMBIGUOUS"}
    assert score["identity_candidate_metrics"]["recall"] == 0


def test_multiple_original_clauses_for_one_prediction_remain_ambiguous_without_pair_optimization():
    gold, prediction = frozen_example("SAME_ENTITY_EXPLICIT")
    duplicate = deepcopy(gold["nodes"][0])
    duplicate["id"] = "G1/lesion:02"
    gold["nodes"].append(duplicate)
    gold["pairs"] = [{"left_id": first["id"], "right_id": second["id"], "label":
                      "INELIGIBLE_SAME_REPORT" if first["report_id"] == second["report_id"] else "SAME_ENTITY_EXPLICIT"}
                     for first, second in combinations(gold["nodes"], 2)]
    score, audit = evaluate_relations(gold, prediction)
    assert score["observations"]["mapped"] == 1 and score["observations"]["missing"] == 2
    assert next(row for row in audit["mappings"] if row["observation_id"] == "O1")["status"] == "AMBIGUOUS"


def test_incompatible_pair_unverified_reason_and_automatic_confirmation_are_separate_failures():
    gold, prediction = frozen_example("INCOMPATIBLE_EXPLICIT_ANATOMY")
    prediction["proposals"][0]["reasons"][0]["field_ids"] = ["F1", "FOREIGN"]
    prediction["proposals"][0]["status"] = "CONFIRMED"
    prediction["stable_lesions"] = [{"id": "L1"}]
    prediction["assignments"] = [{"status": "CONFIRMED", "lesion_id": "L1"}]
    score, audit = evaluate_relations(gold, prediction)
    assert score["proposals"]["incompatible"] == 1
    assert score["reason_sources"]["unverified"] == 1 and score["automatic_confirmation_failures"] == 3
    assert score["identity_candidate_metrics"]["precision"] is None


def test_outside_report_range_and_missing_input_observations_remain_in_coverage():
    gold, prediction = frozen_example()
    prediction["observations"][0]["report_pages"] = [2]
    score, audit = evaluate_relations(gold, prediction)
    assert score["observations"]["outside_scope"] == 1 and score["observations"]["missing"] == 1
    assert score["proposals"]["by_label"] == {"OUTSIDE_SCOPE_ENDPOINT": 1}
    prediction["observations"] = []
    prediction["proposals"] = []
    score, audit = evaluate_relations(gold, prediction)
    assert score["observations"]["gold"] == score["observations"]["missing"] == 2


@pytest.mark.parametrize("changed", ["source_sha256", "ocr_sha256", "patient_group"])
def test_changed_source_or_patient_group_is_rejected_before_mapping(changed):
    gold, prediction = frozen_example()
    prediction["sources"][0][changed] = "different"
    with pytest.raises(ValueError):
        evaluate_relations(gold, prediction)


def test_first_execution_requires_separate_approval_bound_to_all_concrete_identities():
    identities = {name: str(index) * 64 for index, name in enumerate(
        ("gold_sha256", "protocol_sha256", "mapper_sha256", "application_sha256", "manifest_sha256"), 1)}
    approval = {"status": "APPROVED_FIRST_RELATION_EXECUTION", "relation_prediction_authorized": True, "identities": identities}
    validate_execution_approval(approval, identities)
    for key in identities:
        with pytest.raises(ValueError):
            validate_execution_approval(approval, {**identities, key: "changed"})
    with pytest.raises(ValueError):
        validate_execution_approval({**approval, "relation_prediction_authorized": False}, identities)


def synthetic_replay_sources(tmp_path, *, clause_terminator="。"):
    from dataclasses import asdict
    import hashlib
    import json
    from tests.labs.test_phase_two_layout import page

    sources = []
    for index in (1, 2, 3):
        source = tmp_path / f"source-{index}.bin"
        source.write_bytes(f"synthetic relation original {index}".encode())
        identity = hashlib.sha256(source.read_bytes()).hexdigest()
        ocr_page = page([(.1, [(.1, "合成医院 CT诊断报告书")]),
                         (.2, [(.1, f"检查日期：2026-08-0{index} 检查项目：胸部CT平扫")]),
                         (.3, [(.1, f"影像表现：左肺上叶见结节，长径12mm{clause_terminator}")]),
                         (.4, [(.1, "诊断意见：建议随访。")])])
        encoded = asdict(ocr_page)
        encoded["provider_metadata"] = dict(ocr_page.provider_metadata)
        cache = tmp_path / f"{identity}.json"
        cache.write_text(json.dumps({"source_file_hash": identity, "pages": [encoded]}), encoding="utf-8")
        sources.append({"source_number": index, "source_sha256": identity, "source_path": str(source),
                        "ocr_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(), "ocr_path": str(cache)})
    return sources


@pytest.mark.django_db(transaction=True)
def test_synthetic_frozen_replay_persists_separate_patients_and_pending_proposals_without_confirmation(tmp_path):
    from apps.facts.models import FactRevision
    from apps.lesions.models import LesionMatchProposal
    from apps.patients.models import Patient
    from tools.lesion_relation_evaluation import predict_relations

    sources = synthetic_replay_sources(tmp_path)
    prediction, execution = predict_relations({"sources": sources}, {1: "P01", 2: "P01", 3: "P02"})
    assert Patient.objects.count() == 2 and len(prediction["observations"]) == 3
    assert len(prediction["proposals"]) == LesionMatchProposal.objects.count() == 1
    assert prediction["proposals"][0]["status"] == "PENDING"
    assert "unconfirmed_source" in prediction["proposals"][0]["blockers"]
    assert prediction["stable_lesions"] == prediction["assignments"] == [] and not FactRevision.objects.exists()
    assert {field["status"] for row in prediction["observations"] for field in row["fields"]} == {"PENDING"}
    assert execution["database"] == "sqlite" and execution["persistence_and_comparison"]
    assert execution["original_patient_groups"] == 2
    assert set(execution["field_status_counts"]) == {"PENDING"} and execution["field_revision_count"] == 0


def cli_input_files(tmp_path):
    import hashlib
    import json

    gold, _ = frozen_example()
    protocol = tmp_path / "protocol.txt"
    protocol.write_text("synthetic original-only relation protocol", encoding="utf-8")
    gold["policy"] = {"status": "FROZEN_PENDING_INDEPENDENT_REVIEW", "real_relation_predictions_run": False,
                       "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest()}
    sources = []
    for report in gold["reports"]:
        original = tmp_path / (report["id"] + ".bin")
        cache = tmp_path / (report["id"] + ".json")
        original.write_bytes(report["id"].encode())
        cache.write_text('{"pages": []}', encoding="utf-8")
        report["source_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
        report["ocr_sha256"] = hashlib.sha256(cache.read_bytes()).hexdigest()
        sources.append({key: report[key] for key in ("source_number", "source_sha256", "ocr_sha256")}
                       | {"source_path": str(original), "ocr_path": str(cache)})
    gold_path, manifest_path = tmp_path / "gold.json", tmp_path / "manifest.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    manifest_path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    return ["--gold", str(gold_path), "--gold-sha256", hashlib.sha256(gold_path.read_bytes()).hexdigest(),
            "--protocol", str(protocol), "--manifest", str(manifest_path)]


def test_cli_description_does_not_execute_and_keeps_existing_evidence_immutable(tmp_path, monkeypatch):
    import json
    from tools import lesion_relation_evaluation as evaluator

    def forbidden(*args, **kwargs):
        raise AssertionError("Description must never generate a prediction")
    monkeypatch.setattr(evaluator, "predict_relations", forbidden)
    report = tmp_path / "execution-request.json"
    arguments = cli_input_files(tmp_path) + ["--describe", "--report", str(report)]
    assert evaluator.main(arguments) == 0
    description = json.loads(report.read_text(encoding="utf-8"))
    assert description["status"] == "AWAITING_INDEPENDENT_EXECUTION_APPROVAL"
    assert description["real_relation_predictions_run"] is False
    assert set(description["identities"]) == evaluator.IDENTITY_KEYS
    previous = report.read_bytes()
    with pytest.raises(ValueError):
        evaluator.main(arguments)
    assert report.read_bytes() == previous


def test_cli_missing_approval_stops_before_database_or_output_creation(tmp_path, monkeypatch):
    from tools import lesion_relation_evaluation as evaluator

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing approval must stop before a prediction")
    monkeypatch.setattr(evaluator, "predict_relations", forbidden)
    report, private = tmp_path / "report.json", tmp_path / "private"
    arguments = cli_input_files(tmp_path) + ["--report", str(report), "--private-output", str(private)]
    with pytest.raises(ValueError, match="approval"):
        evaluator.main(arguments)
    assert not report.exists() and not private.exists()


def test_cli_synthetic_replay_and_retained_rescore_bind_sources_without_changing_old_artifacts(tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    from tools import lesion_relation_evaluation as evaluator

    sources = synthetic_replay_sources(tmp_path, clause_terminator="")
    gold, _ = frozen_example()
    # Declared independently of the actual pipeline below: two available reports
    # have only unjudged same-location evidence; the third is another patient.
    for source in sources:
        number = source["source_number"]
        if number == 3:
            gold["reports"].append(deepcopy(gold["reports"][0]))
            gold["nodes"].append(deepcopy(gold["nodes"][0]))
        report, node = gold["reports"][number - 1], gold["nodes"][number - 1]
        group = "P02" if number == 3 else "P01"
        report.update({key: source[key] for key in ("source_number", "source_sha256", "ocr_sha256")})
        report.update(id=f"G{number}", patient_group=group)
        node.update(id=f"G{number}/lesion:01", report_id=report["id"], source_number=number, patient_group=group)
        node["local_identity_source"]["value"]["text"] = "左肺上叶"
        node["local_identity_source"]["sources"][0]["raw_quote"] = "左肺上叶见结节，长径12mm"
        node["original_fields"] = [deepcopy(node["local_identity_source"])]
    gold["pairs"] = [{"left_id": first["id"], "right_id": second["id"], "label":
                      "UNJUDGED_WHETHER_SAME_ENTITY" if first["patient_group"] == second["patient_group"]
                      else "INCOMPATIBLE_PATIENT_BOUNDARY"} for first, second in combinations(gold["nodes"], 2)]
    protocol = tmp_path / "synthetic-protocol.txt"
    protocol.write_text("Synthetic original-only CLI replay fixture; no private originals or execution authority.", encoding="utf-8")
    gold["policy"] = {"status": "FROZEN_SYNTHETIC_TEST", "real_relation_predictions_run": False,
                       "protocol_sha256": evaluator._hash(protocol)}
    gold_path, manifest = tmp_path / "synthetic-gold.json", tmp_path / "manifest.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    arguments = ["--gold", str(gold_path), "--gold-sha256", evaluator._hash(gold_path),
                 "--protocol", str(protocol), "--manifest", str(manifest)]
    request = tmp_path / "request.json"
    assert evaluator.main(arguments + ["--describe", "--report", str(request)]) == 0
    approval = tmp_path / "synthetic-test-approval.json"
    approval.write_text(json.dumps({"status": "APPROVED_FIRST_RELATION_EXECUTION",
        "relation_prediction_authorized": True, "identities": json.loads(request.read_text())["identities"],
        "scope": "This automated test's three synthetic byte fixtures only"}), encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    report, private = tmp_path / "generated.json", tmp_path / "generated"
    result = subprocess.run([sys.executable, "-X", "utf8", "tools/lesion_relation_evaluation.py", *arguments,
        "--approval", str(approval), "--approval-sha256", evaluator._hash(approval),
        "--report", str(report), "--private-output", str(private)], cwd=root,
        env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    first = json.loads(report.read_text(encoding="utf-8"))
    prediction = json.loads((private / "predictions.json").read_text(encoding="utf-8"))
    assert first["files"] == {"total": 3, "failed": 0, "unparsed_pages": 0}
    assert first["current"]["pair_universe"]["total"] == 3
    assert first["current"]["observations"]["gold"] == first["current"]["observations"]["mapped"] == 3
    assert first["current"]["proposals"]["by_label"] == {"UNJUDGED_WHETHER_SAME_ENTITY": 1}
    assert first["current"]["proposals"]["by_status"] == {"PENDING": 1}
    assert first["current"]["automatic_confirmation_failures"] == first["execution"]["field_revision_count"] == 0
    assert prediction["stable_lesions"] == prediction["assignments"] == []
    for name, identity in first["identity"]["source_files"].items():
        assert evaluator._hash(private / "source_snapshot" / name) == identity
    original_hashes = {path: evaluator._hash(path) for path in (report, private / "predictions.json", private / "assignments.json")}
    rescore, retained = tmp_path / "rescored.json", tmp_path / "rescored"
    assert evaluator.main(arguments + ["--predictions", str(private / "predictions.json"),
        "--prediction-report", str(report), "--report", str(rescore), "--private-output", str(retained)]) == 0
    second = json.loads(rescore.read_text(encoding="utf-8"))
    assert second["execution_kind"] == "retained_prediction_rescore" and second["current"] == first["current"]
    assert second["identity"]["original_generation_report_sha256"] == original_hashes[report]
    assert (retained / "predictions.json").read_bytes() == (private / "predictions.json").read_bytes()
    assert all(evaluator._hash(path) == identity for path, identity in original_hashes.items())
    tampered = tmp_path / "changed-prediction.json"
    tampered.write_bytes((private / "predictions.json").read_bytes() + b" ")
    rejected = tmp_path / "must-not-be-created"
    with pytest.raises(ValueError, match="Retained predictions"):
        evaluator.main(arguments + ["--predictions", str(tampered), "--prediction-report", str(report),
            "--report", str(rejected.with_suffix(".json")), "--private-output", str(rejected)])
    assert not rejected.exists() and not rejected.with_suffix(".json").exists()


def test_explicit_positive_synthetic_relation_scores_only_its_verified_source():
    gold, prediction = frozen_example("SAME_ENTITY_EXPLICIT")
    score, _ = evaluate_relations(gold, prediction)
    assert score["identity_candidate_metrics"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0, "positive_gold": 1}
    prediction["proposals"][0]["reasons"][0]["field_ids"] = ["F1"]
    score, _ = evaluate_relations(gold, prediction)
    assert score["identity_candidate_metrics"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "positive_gold": 1}


@pytest.mark.parametrize("changed", ["start_offset", "polygon"])
def test_explicit_frozen_locator_must_match_in_addition_to_original_quote(changed):
    gold, prediction = frozen_example()
    fragment = prediction["observations"][0]["fields"][0]["fragments"][0]
    fragment.update(reading_order=7, polygon=[[.1, .2], [.3, .2], [.3, .4], [.1, .4]])
    source = gold["nodes"][0]["local_identity_source"]["sources"][0]
    source["ocr_offsets"] = {key: fragment[key] for key in ("reading_order", "start_offset", "end_offset")}
    source["exact_polygon"] = deepcopy(fragment["polygon"])
    assert evaluate_relations(gold, prediction)[0]["observations"]["mapped"] == 2
    fragment[changed] = 9 if changed == "start_offset" else [[.6, .2], [.8, .2], [.8, .4], [.6, .4]]
    assert evaluate_relations(gold, prediction)[0]["observations"]["mapped"] == 1


def test_verified_reference_date_is_only_literal_reason_not_a_confirmed_study_or_entity():
    gold, prediction = frozen_example()
    for index, key in enumerate(("comparison.reference_date", "report.exam_date")):
        value = {"value": "2026-08-01", "precision": "DAY"}
        quote = "对比2026-08-01胸部CT" if index == 0 else "检查日期：2026-08-01"
        expected = {"field_key": key, "value": value, "sources": [{"page": 1, "raw_quote": quote}]}
        gold["reports"][index]["original_context_fields"].append(expected)
        prediction["observations"][index]["context_fields"].append({"id": f"DATE{index}", "field_key": key,
            "content": {"value": deepcopy(value)}, "source_valid": True, "fragments": [{"page": 1, "raw_text": quote}]})
    gold["reports"][0]["reference_resolution"] = "REFERENCE_NOT_RESOLVED"
    prediction["proposals"][0]["reasons"].append({"code": "reference_date_equal", "field_ids": ["DATE0", "DATE1"]})
    score, audit = evaluate_relations(gold, prediction)
    assert audit["proposals"][0]["source_verified"] and score["proposals"]["by_label"] == {"UNJUDGED_WHETHER_SAME_ENTITY": 1}
    assert score["identity_candidate_metrics"]["precision"] is None
    prediction["observations"][0]["context_fields"][0]["content"]["value"]["precision"] = "MONTH"
    score, audit = evaluate_relations(gold, prediction)
    assert not audit["proposals"][0]["source_verified"]
