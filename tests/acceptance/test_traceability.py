from copy import deepcopy
from pathlib import Path
import subprocess

from tools.verify_traceability import (
    REPORT_PATH,
    expected_ids,
    load_matrix,
    render_markdown,
    validate_matrix,
)


ROOT = Path(__file__).resolve().parents[2]
PRD_PATH = "docs/product/第一版产品需求文档-PRD-v1.0.md"


def test_prd_traceability_is_complete_current_and_has_real_evidence_nodes():
    matrix = load_matrix()

    assert {item["id"] for item in matrix["requirements"]} == expected_ids()
    assert validate_matrix(matrix) == ()
    assert REPORT_PATH.read_text(encoding="utf-8") == render_markdown(matrix)


def test_prd_checkout_uses_lf_for_stable_byte_hashing():
    completed = subprocess.run(
        ["git", "check-attr", "eol", "--", PRD_PATH],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.rstrip().endswith(": eol: lf")


def test_traceability_verifier_rejects_a_missing_must_ac_or_scenario():
    matrix = load_matrix()
    for missing_id in ("MUST-13", "AC-22", "SCN-26"):
        incomplete = deepcopy(matrix)
        incomplete["requirements"] = [
            item for item in incomplete["requirements"] if item["id"] != missing_id
        ]

        errors = validate_matrix(incomplete)

        assert any(missing_id in error for error in errors)


def test_traceability_source_and_evidence_paths_cannot_be_redirected_outside_the_contract():
    wrong_source = load_matrix()
    wrong_source["source"] = "docs/verification/README.md"
    assert any("source must remain" in error for error in validate_matrix(wrong_source))

    unsafe_evidence = load_matrix()
    unsafe_evidence["requirements"][0]["evidence"] = ["../outside.py::test_forged"]
    assert any("unsafe evidence path" in error for error in validate_matrix(unsafe_evidence))


def test_safari_requirements_cannot_be_marked_verified_without_external_evidence():
    matrix = load_matrix()
    for item in matrix["requirements"]:
        if item["id"] in {"AC-22", "SCN-26"}:
            item["status"] = "verified"

    errors = validate_matrix(matrix)

    assert any("AC-22 requires real Safari evidence" in error for error in errors)
    assert any("SCN-26 requires real Safari evidence" in error for error in errors)


def test_safari_requirements_can_be_verified_after_machine_checked_external_evidence():
    matrix = load_matrix()
    for item in matrix["requirements"]:
        if item["id"] in {"AC-22", "SCN-26"}:
            item["status"] = "verified"

    assert validate_matrix(matrix, external_browser_ready=True) == ()
