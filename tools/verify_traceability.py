import argparse
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs" / "verification" / "traceability.json"
REPORT_PATH = ROOT / "docs" / "verification" / "traceability.md"
EXPECTED_SOURCE = "第一版产品需求文档-PRD-v1.0.md"
ALLOWED_STATUSES = {"verified", "external_pending"}
EXTERNAL_BROWSER_GATES = {
    "browser_chrome_current",
    "browser_chrome_previous_1",
    "browser_chrome_previous_2",
    "browser_edge_current",
    "browser_edge_previous_1",
    "browser_edge_previous_2",
    "browser_safari_current",
    "browser_safari_previous",
}


def expected_ids():
    return {
        *(f"MUST-{number:02d}" for number in range(1, 14)),
        *(f"AC-{number:02d}" for number in range(23)),
        *(f"SCN-{number:02d}" for number in range(1, 27)),
    }


def load_matrix(path=MATRIX_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _source_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence_error(root, reference):
    path_text, separator, node = reference.partition("::")
    if "\\" in path_text:
        return f"unsafe evidence path: {reference}"
    evidence_path = (root / path_text).resolve()
    try:
        evidence_path.relative_to(root.resolve())
    except ValueError:
        return f"unsafe evidence path: {reference}"
    if not evidence_path.is_file():
        return f"evidence file does not exist: {reference}"
    if not separator:
        return None
    function_name = node.split("::")[-1].split("[")[0]
    if not re.fullmatch(r"test_[A-Za-z0-9_]+", function_name):
        return f"invalid pytest node: {reference}"
    source = evidence_path.read_text(encoding="utf-8")
    if re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(function_name)}\s*\(", source, re.MULTILINE) is None:
        return f"pytest function does not exist: {reference}"
    return None


def _external_browser_evidence_ready(root):
    try:
        try:
            from tools.verify_release_gate import validate_gate
        except ModuleNotFoundError:
            from verify_release_gate import validate_gate
        release_path = root / "docs" / "verification" / "release-evidence.json"
        release_data = json.loads(release_path.read_text(encoding="utf-8"))
    except (ImportError, OSError, json.JSONDecodeError):
        return False
    statuses = {
        gate.get("id"): gate.get("status")
        for gate in release_data.get("gates", [])
        if isinstance(gate, dict)
    }
    return not validate_gate(release_data, root=root) and all(
        statuses.get(gate_id) == "passed" for gate_id in EXTERNAL_BROWSER_GATES
    )


def validate_matrix(data, root=ROOT, *, external_browser_ready=None):
    errors = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    source_value = data.get("source")
    if source_value != EXPECTED_SOURCE:
        errors.append(f"source must remain {EXPECTED_SOURCE}")
    else:
        source_path = (root / source_value).resolve()
        try:
            source_path.relative_to(root.resolve())
        except ValueError:
            errors.append("source path escapes the repository")
            source_path = None
        if source_path is None:
            pass
        elif not source_path.is_file():
            errors.append(f"source file does not exist: {source_value}")
        elif data.get("source_sha256") != _source_digest(source_path):
            errors.append("source_sha256 does not match the PRD")

    requirements = data.get("requirements")
    if not isinstance(requirements, list):
        return (*errors, "requirements must be a list")
    identifiers = [item.get("id") for item in requirements if isinstance(item, dict)]
    duplicates = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
    if duplicates:
        errors.append(f"duplicate requirement ids: {', '.join(duplicates)}")
    missing = sorted(expected_ids() - set(identifiers))
    unexpected = sorted(set(identifiers) - expected_ids())
    if missing:
        errors.append(f"missing requirement ids: {', '.join(missing)}")
    if unexpected:
        errors.append(f"unexpected requirement ids: {', '.join(unexpected)}")

    browser_ready = external_browser_ready
    if browser_ready is None:
        needs_external_check = any(
            isinstance(item, dict)
            and item.get("id") in {"AC-22", "SCN-26"}
            and item.get("status") == "verified"
            for item in requirements
        )
        browser_ready = _external_browser_evidence_ready(root) if needs_external_check else False

    for item in requirements:
        if not isinstance(item, dict):
            errors.append("every requirement must be an object")
            continue
        identifier = item.get("id", "<missing>")
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            errors.append(f"{identifier} has no title")
        if not isinstance(item.get("section"), str) or not item["section"].strip():
            errors.append(f"{identifier} has no PRD section")
        status = item.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{identifier} has invalid status: {status}")
        if status == "external_pending" and identifier not in {"AC-22", "SCN-26"}:
            errors.append(f"{identifier} cannot be external_pending")
        if status == "verified" and identifier in {"AC-22", "SCN-26"} and not browser_ready:
            errors.append(f"{identifier} requires real Safari evidence before verification")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{identifier} has no evidence")
            continue
        if status == "verified" and not any(reference.startswith("tests/") for reference in evidence):
            errors.append(f"{identifier} has no executable test evidence")
        for reference in evidence:
            if not isinstance(reference, str) or not reference:
                errors.append(f"{identifier} has an invalid evidence reference")
                continue
            evidence_error = _evidence_error(root, reference)
            if evidence_error:
                errors.append(f"{identifier}: {evidence_error}")
    return tuple(errors)


def render_markdown(data):
    requirements = data["requirements"]
    verified = sum(item["status"] == "verified" for item in requirements)
    pending = sum(item["status"] == "external_pending" for item in requirements)
    lines = [
        "# PRD v1.0 需求追踪矩阵",
        "",
        f"源文件 SHA-256：`{data['source_sha256']}`",
        "",
        f"共 {len(requirements)} 项：自动验证 {verified} 项，外部待验证 {pending} 项。",
        "`verified` 表示存在可执行自动化证据或经哈希证明的外部浏览器证据；`external_pending` 不计为发布通过。",
        "",
    ]
    groups = (
        ("MUST 功能", "MUST-"),
        ("验收标准", "AC-"),
        ("测试场景", "SCN-"),
    )
    for heading, prefix in groups:
        lines.extend((f"## {heading}", "", "| 编号 | PRD 章节 | 状态 | 要求 | 证据 |", "| --- | --- | --- | --- | --- |"))
        for item in requirements:
            if not item["id"].startswith(prefix):
                continue
            title = item["title"].replace("|", "\\|")
            evidence = "<br>".join(f"`{reference}`" for reference in item["evidence"])
            lines.append(
                f"| {item['id']} | {item['section']} | {item['status']} | {title} | {evidence} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate the PRD traceability matrix")
    parser.add_argument("--write", action="store_true", help="regenerate the Markdown report")
    args = parser.parse_args(argv)
    data = load_matrix()
    errors = validate_matrix(data)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    report = render_markdown(data)
    if args.write:
        REPORT_PATH.write_text(report, encoding="utf-8", newline="\n")
    elif not REPORT_PATH.is_file() or REPORT_PATH.read_text(encoding="utf-8") != report:
        print("ERROR: traceability.md is stale; run with --write")
        return 1
    counts = {
        status: sum(item["status"] == status for item in data["requirements"])
        for status in sorted(ALLOWED_STATUSES)
    }
    print(f"Traceability verified: {len(data['requirements'])} requirements, {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
