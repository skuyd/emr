from copy import deepcopy

from tools.verify_traceability import (
    REPORT_PATH,
    expected_ids,
    load_matrix,
    render_markdown,
    validate_matrix,
)


def test_prd_traceability_is_complete_current_and_has_real_evidence_nodes():
    matrix = load_matrix()

    assert {item["id"] for item in matrix["requirements"]} == expected_ids()
    assert validate_matrix(matrix) == ()
    assert REPORT_PATH.read_text(encoding="utf-8") == render_markdown(matrix)


def test_traceability_verifier_rejects_a_missing_must_ac_or_scenario():
    matrix = load_matrix()
    for missing_id in ("MUST-13", "AC-22", "SCN-26"):
        incomplete = deepcopy(matrix)
        incomplete["requirements"] = [
            item for item in incomplete["requirements"] if item["id"] != missing_id
        ]

        errors = validate_matrix(incomplete)

        assert any(missing_id in error for error in errors)


def test_safari_requirements_cannot_be_marked_verified_without_external_evidence():
    matrix = load_matrix()
    for item in matrix["requirements"]:
        if item["id"] in {"AC-22", "SCN-26"}:
            item["status"] = "verified"

    errors = validate_matrix(matrix)

    assert any("AC-22 requires real Safari evidence" in error for error in errors)
    assert any("SCN-26 requires real Safari evidence" in error for error in errors)
