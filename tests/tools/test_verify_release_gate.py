import copy

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
