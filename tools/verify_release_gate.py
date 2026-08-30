from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "docs" / "verification" / "release-evidence.json"
REPORT_PATH = ROOT / "docs" / "verification" / "release-gate.md"
ALLOWED_STATUSES = {"passed", "pending"}

REQUIRED_GATES = {
    "prd_traceability": "PRD 需求追踪",
    "indicator_dictionary": "125 项指标字典",
    "python_regression": "Python 全量回归",
    "javascript_regression": "Service Worker JavaScript 回归",
    "production_configuration": "生产配置检查",
    "privacy_security": "隐私与安全回归",
    "deployment_contracts": "部署静态契约",
    "browser_chrome_current": "Chrome 当前主版本完整流程",
    "browser_chrome_previous_1": "Chrome 前一主版本完整流程",
    "browser_chrome_previous_2": "Chrome 前二主版本完整流程",
    "browser_edge_current": "Edge 当前主版本完整流程",
    "browser_edge_previous_1": "Edge 前一主版本完整流程",
    "browser_edge_previous_2": "Edge 前二主版本完整流程",
    "browser_safari_current": "Safari 当前主版本完整流程",
    "browser_safari_previous": "Safari 前一主版本完整流程",
    "accessibility_manual": "跨浏览器无障碍人工验收",
    "postgres_concurrency": "PostgreSQL 并发事务验证",
    "offline_ocr_model": "离线 PaddleOCR 模型实跑",
    "production_sms": "真实生产短信网关",
    "private_object_storage": "真实私有 S3",
    "multi_process_stack": "多进程 Web/Worker/Beat 部署",
    "fixed_load_performance": "固定负载性能",
    "backup_restore": "加密备份恢复与删除账本演练",
}

ATTESTATION_GATES = {
    "browser_chrome_current",
    "browser_chrome_previous_1",
    "browser_chrome_previous_2",
    "browser_edge_current",
    "browser_edge_previous_1",
    "browser_edge_previous_2",
    "browser_safari_current",
    "browser_safari_previous",
    "accessibility_manual",
    "postgres_concurrency",
    "offline_ocr_model",
    "production_sms",
    "private_object_storage",
    "multi_process_stack",
    "fixed_load_performance",
    "backup_restore",
}


def load_gate(path=GATE_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _safe_path(root, value):
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    candidate = (root / value.partition("::")[0]).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _valid_iso_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def _validate_attestation(root, gate_id, reference):
    errors = []
    expected = f"docs/verification/attestations/{gate_id}.json"
    if reference != expected:
        return [f"{gate_id} attestation must be {expected}"]
    path = _safe_path(root, reference)
    if path is None or not path.is_file():
        return [f"{gate_id} attestation file does not exist"]
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"{gate_id} attestation is not valid JSON"]
    if record.get("schema_version") != 1 or record.get("gate_id") != gate_id:
        errors.append(f"{gate_id} attestation identity is invalid")
    if record.get("status") != "passed":
        errors.append(f"{gate_id} attestation status must be passed")
    if not _valid_iso_time(record.get("executed_at")):
        errors.append(f"{gate_id} attestation needs a timezone-aware executed_at")
    for field in ("environment", "command", "result"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            errors.append(f"{gate_id} attestation needs {field}")
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append(f"{gate_id} attestation needs hashed artifacts")
        return errors
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            errors.append(f"{gate_id} attestation has an invalid artifact entry")
            continue
        artifact_path = _safe_path(root, artifact["path"])
        if artifact_path is None or not artifact_path.is_file():
            errors.append(f"{gate_id} attestation artifact does not exist: {artifact['path']}")
            continue
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"] or "") or digest != artifact["sha256"]:
            errors.append(f"{gate_id} attestation artifact hash mismatch: {artifact['path']}")
    return errors


def validate_gate(data, root=ROOT):
    errors = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not _valid_iso_time(data.get("generated_at")):
        errors.append("generated_at must be timezone-aware ISO-8601")
    gates = data.get("gates")
    if not isinstance(gates, list):
        return (*errors, "gates must be a list")
    identifiers = [gate.get("id") for gate in gates if isinstance(gate, dict)]
    duplicates = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
    missing = sorted(set(REQUIRED_GATES) - set(identifiers))
    unexpected = sorted(set(identifiers) - set(REQUIRED_GATES))
    if duplicates:
        errors.append(f"duplicate gate ids: {', '.join(duplicates)}")
    if missing:
        errors.append(f"missing gate ids: {', '.join(missing)}")
    if unexpected:
        errors.append(f"unexpected gate ids: {', '.join(unexpected)}")

    for gate in gates:
        if not isinstance(gate, dict):
            errors.append("every gate must be an object")
            continue
        gate_id = gate.get("id", "<missing>")
        if gate.get("title") != REQUIRED_GATES.get(gate_id):
            errors.append(f"{gate_id} title does not match the required gate")
        if gate.get("required") is not True:
            errors.append(f"{gate_id} must remain required")
        status = gate.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{gate_id} has invalid status: {status}")
        evidence = gate.get("evidence", [])
        if not isinstance(evidence, list):
            errors.append(f"{gate_id} evidence must be a list")
            evidence = []
        for reference in evidence:
            path = _safe_path(root, reference)
            if path is None or not path.is_file():
                errors.append(f"{gate_id} evidence does not exist or is unsafe: {reference}")
        if status == "passed":
            if not evidence:
                errors.append(f"{gate_id} passed without evidence")
            for field in ("command", "result"):
                if not isinstance(gate.get(field), str) or not gate[field].strip():
                    errors.append(f"{gate_id} passed without {field}")
            if gate_id in ATTESTATION_GATES:
                errors.extend(_validate_attestation(root, gate_id, gate.get("attestation")))
        elif status == "pending":
            if not isinstance(gate.get("blocker"), str) or not gate["blocker"].strip():
                errors.append(f"{gate_id} pending without a blocker")
            if gate.get("attestation"):
                errors.append(f"{gate_id} pending gate must not claim an attestation")

    all_passed = len(gates) == len(REQUIRED_GATES) and all(
        isinstance(gate, dict) and gate.get("status") == "passed" for gate in gates
    )
    expected_decision = "PASS" if all_passed else "BLOCKED"
    if data.get("decision") != expected_decision:
        errors.append(f"decision must be {expected_decision} for the current gate statuses")
    return tuple(errors)


def render_markdown(data):
    gates = data["gates"]
    passed = sum(gate["status"] == "passed" for gate in gates)
    pending = sum(gate["status"] == "pending" for gate in gates)
    lines = [
        "# V1 上线放行门禁",
        "",
        f"**结论：{data['decision']}**",
        "",
        f"生成时间：`{data['generated_at']}`。必需门禁 {len(gates)} 项，通过 {passed} 项，待验证 {pending} 项。",
        "",
        "本报告由 `tools/verify_release_gate.py --write` 从 `release-evidence.json` 生成。",
        "只要一个必需项仍为 `pending`，结论就只能是 `BLOCKED`；手工修改本页不能放行。",
        "",
        "| 门禁 | 状态 | 结果或阻断原因 | 证据 |",
        "| --- | --- | --- | --- |",
    ]
    for gate in gates:
        detail = gate.get("result") if gate["status"] == "passed" else gate.get("blocker")
        detail = str(detail).replace("|", "\\|")
        evidence = "<br>".join(f"`{value}`" for value in gate.get("evidence", ())) or "—"
        lines.append(f"| {gate['title']} | {gate['status']} | {detail} | {evidence} |")
    blockers = [gate for gate in gates if gate["status"] == "pending"]
    if blockers:
        lines.extend(("", "## 放行前待办", ""))
        for gate in blockers:
            lines.append(f"- {gate['title']}：{gate['blocker']}")
    lines.extend(
        (
            "",
            "## 判定规则",
            "",
            "外部环境门禁只有在提交带时区执行时间、环境、命令、结果和制品 SHA-256 的独立证明后才能改为 `passed`。",
            "WebKit 参考运行不能代替真实 Safari；跳过测试、预计结果、空白性能数字和仅有说明文字都不算通过。",
            "",
        )
    )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate the V1 release gate")
    parser.add_argument("--write", action="store_true", help="regenerate release-gate.md")
    args = parser.parse_args(argv)
    data = load_gate()
    errors = validate_gate(data)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    report = render_markdown(data)
    if args.write:
        REPORT_PATH.write_text(report, encoding="utf-8", newline="\n")
    elif not REPORT_PATH.is_file() or REPORT_PATH.read_text(encoding="utf-8") != report:
        print("ERROR: release-gate.md is stale; run with --write")
        return 1
    counts = {status: sum(gate["status"] == status for gate in data["gates"]) for status in sorted(ALLOWED_STATUSES)}
    print(f"Release gate verified: decision={data['decision']}, counts={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
