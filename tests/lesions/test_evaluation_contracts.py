"""Keep every automatic mutation visible and preserve completed raw predictions."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.lesions.test_evaluation import frozen_example
from tools import lesion_relation_evaluation as evaluator


@pytest.mark.parametrize("status", ["UNASSIGNED", "DEFERRED", "REJECTED", "CONFIRMED"])
def test_every_automatic_assignment_revision_is_counted_even_without_a_lesion_id(status):
    gold, prediction = frozen_example()
    prediction["assignments"] = [{"status": status, "lesion_id": None}]
    score, _ = evaluator.evaluate_relations(gold, prediction)
    assert score["automatic_confirmation_failures"] == 1
    assert score["automatic_mutations"]["assignment_revisions"] == 1


def test_field_confirmation_and_all_fact_revisions_are_visible_including_reset_to_pending():
    gold, prediction = frozen_example()
    field = prediction["observations"][0]["fields"][0]
    field["status"] = "CONFIRMED"
    # The same context field may occur on several observations; count its
    # effective state once, while preserving every distinct revision event.
    prediction["observations"][1]["context_fields"].append(deepcopy(field))
    prediction["fact_revisions"] = [
        {"id": "REV1", "fact_id": field["id"], "status": "CONFIRMED"},
        {"id": "REV2", "fact_id": "OTHER", "status": "PENDING"},
    ]
    score, _ = evaluator.evaluate_relations(gold, prediction)
    assert score["automatic_mutations"]["non_pending_fields"] == 1
    assert score["automatic_mutations"]["fact_revisions"] == 2
    assert score["automatic_confirmation_failures"] == 3


def test_pending_proposal_with_a_reverted_automatic_decision_is_still_a_failure():
    gold, prediction = frozen_example()
    prediction["proposal_revisions"] = [
        {"id": "R1", "proposal_id": "P1", "status": "PENDING", "action": "PROPOSE"},
        {"id": "R2", "proposal_id": "P1", "status": "DEFERRED", "action": "DEFER"},
        {"id": "R3", "proposal_id": "P1", "status": "PENDING", "action": "UNDO"},
    ]
    score, _ = evaluator.evaluate_relations(gold, prediction)
    assert score["automatic_mutations"]["proposal_decision_revisions"] == 2
    assert score["automatic_confirmation_failures"] == 2


def test_actual_initial_pending_proposal_revisions_do_not_count_as_user_decisions():
    gold, prediction = frozen_example()
    prediction["proposal_revisions"] = [
        {"id": "R1", "proposal_id": "P1", "status": "PENDING", "action": "PROPOSE"},
    ]
    prediction["fact_revisions"] = []
    score, _ = evaluator.evaluate_relations(gold, prediction)
    assert score["automatic_confirmation_failures"] == 0
    assert score["automatic_mutations"]["proposal_decision_revisions"] == 0


@pytest.mark.django_db(transaction=True)
def test_actual_replay_captures_late_field_revision_outside_local_observation_fields(tmp_path, monkeypatch):
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact
    from apps.lesions import services
    from tests.lesions.test_evaluation import synthetic_replay_sources

    generate = services.generate_proposals

    def illegal_confirmation(patient, *, actor):
        result = generate(patient, actor=actor)
        field = Fact.objects.get(document__patient=patient, field_key="imaging.impression",
                                 document__display_filename__contains=hash_prefix)
        revise_fact(patient, field.pk, actor=actor, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(field)["current_source_token"], checked_original=True)
        return result

    sources = synthetic_replay_sources(tmp_path, clause_terminator="")[:2]
    hash_prefix = sources[0]["source_sha256"][:12]
    monkeypatch.setattr(services, "generate_proposals", illegal_confirmation)
    prediction, execution = evaluator.predict_relations({"sources": sources}, {1: "P01", 2: "P01"})
    gold, _ = frozen_example()
    for source, report in zip(sources, gold["reports"]):
        report.update({key: source[key] for key in ("source_number", "source_sha256", "ocr_sha256")})
    score, _ = evaluator.evaluate_relations(gold, prediction)
    assert execution["field_revision_count"] == 1
    assert execution["field_status_counts"]["CONFIRMED"] == 1
    assert score["automatic_mutations"]["fact_revisions"] == 1
    assert score["automatic_mutations"]["non_pending_fields"] == 1
    assert all(score["mutation_evidence"].values())


def _synthetic_cli_inputs(tmp_path):
    from tests.lesions.test_evaluation import synthetic_replay_sources

    sources = synthetic_replay_sources(tmp_path, clause_terminator="")[:2]
    gold, _ = frozen_example()
    for source, report in zip(sources, gold["reports"]):
        report.update({key: source[key] for key in ("source_number", "source_sha256", "ocr_sha256")})
    protocol = tmp_path / "synthetic-protocol.txt"
    protocol.write_text("Synthetic failure-retention test only; no real source authority.", encoding="utf-8")
    gold["policy"] = {"status": "FROZEN_SYNTHETIC_TEST", "real_relation_predictions_run": False,
                      "protocol_sha256": evaluator._hash(protocol)}
    gold_path, manifest = tmp_path / "gold.json", tmp_path / "manifest.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    manifest.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    arguments = ["--gold", str(gold_path), "--gold-sha256", evaluator._hash(gold_path),
                 "--protocol", str(protocol), "--manifest", str(manifest)]
    request, approval = tmp_path / "request.json", tmp_path / "synthetic-approval.json"
    assert evaluator.main(arguments + ["--describe", "--report", str(request)]) == 0
    approval.write_text(json.dumps({"status": "APPROVED_FIRST_RELATION_EXECUTION",
        "relation_prediction_authorized": True, "identities": json.loads(request.read_text())["identities"],
        "scope": "Only two temporary synthetic original byte fixtures in this test"}), encoding="utf-8")
    return arguments, ["--approval", str(approval), "--approval-sha256", evaluator._hash(approval)]


def _run_cli(arguments, script):
    root = Path(evaluator.__file__).resolve().parents[1]
    return subprocess.run([sys.executable, "-X", "utf8", "-c", script, *arguments], cwd=root,
        env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True, encoding="utf-8", timeout=180)


@pytest.mark.parametrize("stage", ["SCORING", "POST_PREDICTION_VALIDATION"])
def test_actual_predictions_and_execution_identity_survive_later_failure(tmp_path, stage):
    arguments, approval = _synthetic_cli_inputs(tmp_path)
    private, report = tmp_path / "execution", tmp_path / "report.json"
    script = """import sys
from tools import lesion_relation_evaluation as evaluator
STAGE = sys.argv.pop(1)
if STAGE == 'SCORING':
    def failed_score(gold, prediction):
        assert len(prediction['sources']) == len(prediction['observations']) == 2
        raise RuntimeError('synthetic_late_failure')
    evaluator.evaluate_relations = failed_score
else:
    original = evaluator._input_groups
    calls = 0
    def failed_validation(gold, manifest):
        global calls
        calls += 1
        if calls > 1:
            from apps.documents.models import Document
            assert Document.objects.count() == 2
            raise RuntimeError('synthetic_late_failure')
        return original(gold, manifest)
    evaluator._input_groups = failed_validation
evaluator.main(sys.argv[1:])
"""
    result = _run_cli([stage, *arguments, *approval, "--report", str(report), "--private-output", str(private)], script)
    assert result.returncode != 0 and "synthetic_late_failure" in result.stderr
    assert not report.exists()
    saved = json.loads((private / "predictions.json").read_text(encoding="utf-8"))
    assert len(saved["sources"]) == len(saved["observations"]) == 2
    generation = json.loads((private / "generation.json").read_text(encoding="utf-8"))
    failure = json.loads((private / "failure.json").read_text(encoding="utf-8"))
    assert generation["status"] == "PREDICTIONS_CAPTURED_UNSCORED"
    assert generation["execution"]["original_patient_groups"] == 1
    assert generation["identity"]["prediction_content_sha256"] == evaluator._hash(private / "predictions.json")
    assert failure["status"] == "FAILED" and failure["stage"] == stage
    assert failure["prediction_captured"] is True
    assert failure["generation_receipt_sha256"] == evaluator._hash(private / "generation.json")
    assert failure["prediction_content_sha256"] == evaluator._hash(private / "predictions.json")
    assert "current" not in failure and "current" not in generation


def test_failed_rescore_keeps_original_generation_and_prediction_bytes_unchanged(tmp_path, monkeypatch):
    arguments, approval = _synthetic_cli_inputs(tmp_path)
    private, report = tmp_path / "generated", tmp_path / "generated.json"
    result = _run_cli([*arguments, *approval, "--report", str(report), "--private-output", str(private)],
        "import sys; from tools import lesion_relation_evaluation as e; e.main(sys.argv[1:])")
    assert result.returncode == 0, result.stdout + result.stderr
    paths = [report, private / "predictions.json", private / "assignments.json", private / "generation.json"]
    before = {path: evaluator._hash(path) for path in paths}

    def fail(gold, prediction):
        raise RuntimeError("synthetic_rescore_failure")

    monkeypatch.setattr(evaluator, "evaluate_relations", fail)
    retained, rescored = tmp_path / "rescore", tmp_path / "rescored.json"
    with pytest.raises(RuntimeError, match="synthetic_rescore_failure"):
        evaluator.main(arguments + ["--predictions", str(private / "predictions.json"),
            "--prediction-report", str(report), "--report", str(rescored), "--private-output", str(retained)])
    assert not rescored.exists() and all(evaluator._hash(path) == identity for path, identity in before.items())
    assert (retained / "predictions.json").read_bytes() == (private / "predictions.json").read_bytes()
    generation = json.loads((retained / "generation.json").read_text(encoding="utf-8"))
    assert generation["execution_kind"] == "retained_prediction_rescore"
    assert generation["identity"]["original_generation_report_sha256"] == before[report]
    assert json.loads((retained / "failure.json").read_text())["stage"] == "SCORING"
