import copy
import hashlib
import json

from tools import verify_release_gate

from tools.verify_release_gate import (
    GATE_PATH,
    REQUIRED_GATES,
    load_gate,
    render_markdown,
    validate_gate,
)


def test_checked_in_release_gate_is_complete_and_honestly_blocked():
    data = load_gate()

    assert validate_gate(data) == ()
    assert data["decision"] == "BLOCKED"
    assert {gate["id"] for gate in data["gates"]} == set(REQUIRED_GATES)
    assert any(gate["status"] == "pending" for gate in data["gates"])


def test_release_gate_rejects_missing_required_gate():
    data = load_gate()
    data["gates"] = data["gates"][:-1]

    errors = validate_gate(data)

    assert any("missing gate ids" in error for error in errors)


def test_release_gate_cannot_claim_pass_while_any_gate_is_pending():
    data = load_gate()
    data["decision"] = "PASS"

    errors = validate_gate(data)

    assert "decision must be BLOCKED for the current gate statuses" in errors


def test_external_gate_cannot_pass_from_prose_without_hashed_attestation():
    data = copy.deepcopy(load_gate())
    target = next(gate for gate in data["gates"] if gate["id"] == "production_sms")
    target.update(
        {
            "status": "passed",
            "command": "claimed command",
            "result": "claimed result",
            "evidence": ["tests/accounts/test_sms_gateway.py"],
        }
    )
    target.pop("blocker")

    errors = validate_gate(data)

    assert any("production_sms attestation" in error for error in errors)


def test_generated_release_report_is_not_hand_edited():
    data = load_gate()
    report = GATE_PATH.with_name("release-gate.md")

    assert report.read_text(encoding="utf-8") == render_markdown(data)


def test_validation_can_succeed_while_production_promotion_fails(capsys):
    assert verify_release_gate.main([]) == 0
    assert verify_release_gate.main(["--require-pass"]) == 2
    assert "BLOCKED" in capsys.readouterr().out


def test_production_promotion_accepts_complete_valid_evidence_and_rejects_tampering(tmp_path, monkeypatch):
    data = load_gate()
    data["decision"] = "PASS"
    artifact = tmp_path / "result.txt"
    artifact.write_text("synthetic fixture for validator behavior", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    for gate in data["gates"]:
        gate.update(status="passed", command="fixture verification", result="passed", evidence=["result.txt"])
        gate.pop("blocker", None)
        if gate["id"] in verify_release_gate.ATTESTATION_GATES:
            reference = f"docs/verification/attestations/{gate['id']}.json"
            gate["attestation"] = reference
            path = tmp_path / reference
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema_version": 1, "gate_id": gate["id"], "status": "passed",
                "executed_at": "2026-09-06T00:00:00+00:00", "environment": "isolated test fixture",
                "command": "fixture verification", "result": "passed",
                "artifacts": [{"path": "result.txt", "sha256": digest}],
            }), encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text(render_markdown(data), encoding="utf-8")
    monkeypatch.setattr(verify_release_gate, "load_gate", lambda: data)
    monkeypatch.setattr(verify_release_gate, "REPORT_PATH", report)
    monkeypatch.setattr(verify_release_gate, "validate_gate", lambda value: validate_gate(value, root=tmp_path))
    assert verify_release_gate.main(["--require-pass"]) == 0
    artifact.write_text("tampered fixture", encoding="utf-8")
    assert verify_release_gate.main(["--require-pass"]) == 1
