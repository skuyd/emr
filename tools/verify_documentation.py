from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = "docs/document-registry.json"
INDEX_PATH = "docs/README.md"
POLICY_PATH = "docs/policies/document-governance.md"
RELEASE_GATE_PATH = "docs/verification/release-gate.md"
DOCUMENTATION_COMMAND = "python tools/verify_documentation.py"

REQUIRED_ENTRY_FIELDS = (
    "id",
    "title",
    "path",
    "kind",
    "lifecycle",
    "delivery",
    "owner",
    "releases",
    "supersedes",
    "implementation_refs",
    "evidence",
)
ARRAY_FIELDS = ("releases", "supersedes", "implementation_refs", "evidence")
KINDS = {
    "product",
    "decision",
    "spec",
    "plan",
    "policy",
    "guide",
    "runbook",
    "evidence",
    "release",
    "license",
}
LIFECYCLES = {"draft", "active", "superseded", "archived"}
DELIVERIES = {
    "not_applicable",
    "planned",
    "implementing",
    "implemented",
    "verified",
    "blocked",
}
OWNERS = {"product", "engineering", "qa", "operations", "legal"}
DELIVERABLE_KINDS = {"product", "spec", "plan"}
DELIVERED_STATES = {"implemented", "verified", "blocked"}
EVIDENCE_STATES = {"verified", "blocked"}
ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SEMVER_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
IMPLEMENTATION_REF_PATTERN = re.compile(
    r"(?:commit:[0-9a-f]{7,40}|pr:#[1-9][0-9]*)"
)
DATE_PREFIXED_FILENAME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*\.md"
)
KEBAB_FILENAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
LINK_PATTERN = re.compile(r"\[[^\]]*\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".worktrees",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
}
KIND_PREFIXES = {
    "product": "docs/product/",
    "decision": "docs/decisions/",
    "spec": "docs/specs/",
    "plan": "docs/plans/",
    "policy": "docs/policies/",
    "runbook": "docs/deployment/",
    "evidence": "docs/verification/",
}
DATE_PREFIXED_KINDS = {"decision", "spec", "plan"}
LEGACY_FILENAME_EXCEPTIONS = {
    "docs/product/产品方案-v2.0-评审完善稿.md",
    "docs/product/第一版产品需求文档-PRD-v1.0.md",
    "docs/decisions/方案审查结论.md",
}


class DocumentationError(ValueError):
    pass


def _repository_path(root, relative_path):
    return root.joinpath(*PurePosixPath(relative_path).parts)


def _assert_repository_containment(root, relative_path, *, label):
    path = _repository_path(root, relative_path)
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return
    except (OSError, RuntimeError) as error:
        raise DocumentationError(
            f"could not resolve {label}: {relative_path}: {error}"
        ) from error
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise DocumentationError(
            f"{label} resolves outside repository: {relative_path}"
        ) from error


def _read_text(root, relative_path):
    path = _repository_path(root, relative_path)
    _assert_repository_containment(
        root,
        relative_path,
        label="required documentation file",
    )
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise DocumentationError(
            f"required documentation file is missing: {relative_path}"
        ) from error
    except UnicodeDecodeError as error:
        raise DocumentationError(f"{relative_path} is not valid UTF-8") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise DocumentationError(f"could not read {relative_path}: {detail}") from error


def _load_registry(root):
    try:
        value = json.loads(_read_text(root, REGISTRY_PATH))
    except json.JSONDecodeError as error:
        raise DocumentationError(f"{REGISTRY_PATH} is not valid JSON") from error
    if not isinstance(value, dict):
        raise DocumentationError(f"{REGISTRY_PATH} must contain an object")
    if value.get("schema_version") != 1:
        raise DocumentationError("document registry schema_version must be 1")
    if not isinstance(value.get("documents"), list):
        raise DocumentationError(f"{REGISTRY_PATH} must contain a documents array")
    return value


def _is_normalized_relative_path(value, *, markdown=False):
    if not isinstance(value, str) or not value:
        return False
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    if posixpath.normpath(value) != value:
        return False
    if any(part in {"", ".", ".."} for part in PurePosixPath(value).parts):
        return False
    return not markdown or value.endswith(".md")


def _require_string_list(document_id, field, value):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise DocumentationError(f"{document_id} field {field} must be an array of strings")
    if len(value) != len(set(value)):
        raise DocumentationError(f"{document_id} field {field} contains duplicates")


def _validate_kind_path(document):
    document_id = document["id"]
    path = document["path"]
    kind = document["kind"]
    if document["lifecycle"] == "archived" and path.startswith("docs/archive/"):
        return
    expected_prefix = KIND_PREFIXES.get(kind)
    if expected_prefix is not None:
        if not path.startswith(expected_prefix):
            raise DocumentationError(
                f"{document_id} with kind {kind} must live under {expected_prefix}"
            )
        return
    if kind == "guide":
        if path in {"README.md", "docs/README.md"} or path.startswith(
            "docs/deployment/"
        ):
            return
        raise DocumentationError(
            f"{document_id} with kind guide must be a repository, documentation, or deployment guide"
        )
    if kind == "release":
        if path == "CHANGELOG.md" or path.startswith("docs/releases/"):
            return
        raise DocumentationError(
            f"{document_id} with kind release must be CHANGELOG.md or live under docs/releases/"
        )
    if kind == "license":
        if path.startswith("docs/licenses/") or PurePosixPath(path).name.startswith(
            ("LICENSE", "NOTICE")
        ):
            return
        raise DocumentationError(
            f"{document_id} with kind license must live under docs/licenses/"
        )


def _validate_filename(document):
    document_id = document["id"]
    path = document["path"]
    name = PurePosixPath(path).name
    if path in LEGACY_FILENAME_EXCEPTIONS:
        return
    if path in {"README.md", "CHANGELOG.md", "docs/README.md"} or name == "README.md":
        return
    if len(PurePosixPath(path).parts) == 1 and name.startswith(("LICENSE", "NOTICE")):
        return
    if document["kind"] == "release":
        version = name.removeprefix("v").removesuffix(".md")
        if name.startswith("v") and SEMVER_PATTERN.fullmatch(version) is not None:
            return
        raise DocumentationError(
            f"{document_id} must use a vMAJOR.MINOR.PATCH.md filename"
        )
    if document["kind"] in DATE_PREFIXED_KINDS:
        if DATE_PREFIXED_FILENAME_PATTERN.fullmatch(name) is not None:
            return
        raise DocumentationError(
            f"{document_id} must use a YYYY-MM-DD-kebab-case.md filename"
        )
    if KEBAB_FILENAME_PATTERN.fullmatch(name) is None:
        raise DocumentationError(
            f"{document_id} must use a lowercase kebab-case.md filename"
        )


def _validate_entries(root, registry):
    documents = registry["documents"]
    seen_ids = set()
    seen_paths = set()
    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            raise DocumentationError(f"document entry {index} must be an object")
        label = document.get("id")
        if not isinstance(label, str) or not label:
            label = f"document entry {index}"
        for field in REQUIRED_ENTRY_FIELDS:
            if field not in document:
                raise DocumentationError(f"{label} is missing required field: {field}")

        document_id = document["id"]
        if not isinstance(document_id, str) or ID_PATTERN.fullmatch(document_id) is None:
            raise DocumentationError(f"invalid document id: {document_id}")
        if document_id in seen_ids:
            raise DocumentationError(f"duplicate document id: {document_id}")
        seen_ids.add(document_id)

        title = document["title"]
        if not isinstance(title, str) or not title.strip():
            raise DocumentationError(f"{document_id} title must be a non-empty string")
        enum_values = {
            "kind": KINDS,
            "lifecycle": LIFECYCLES,
            "delivery": DELIVERIES,
            "owner": OWNERS,
        }
        for field, allowed in enum_values.items():
            value = document[field]
            if value not in allowed:
                raise DocumentationError(f"{document_id} has invalid {field}: {value}")

        path = document["path"]
        if not _is_normalized_relative_path(path, markdown=True):
            raise DocumentationError(
                f"{document_id} path must be a normalized repository-relative path"
            )
        if path in seen_paths:
            raise DocumentationError(f"duplicate document path: {path}")
        seen_paths.add(path)
        _validate_kind_path(document)
        _validate_filename(document)
        if not _repository_path(root, path).is_file():
            raise DocumentationError(f"registered Markdown document does not exist: {path}")
        _assert_repository_containment(root, path, label="registered document")

        for field in ARRAY_FIELDS:
            _require_string_list(document_id, field, document[field])
        for version in document["releases"]:
            if SEMVER_PATTERN.fullmatch(version) is None:
                raise DocumentationError(f"{document_id} has invalid release: {version}")
        for reference in document["implementation_refs"]:
            if IMPLEMENTATION_REF_PATTERN.fullmatch(reference) is None:
                raise DocumentationError(
                    f"{document_id} has invalid implementation reference: {reference}"
                )

        if (
            document["kind"] in DELIVERABLE_KINDS
            and document["delivery"] in DELIVERED_STATES
            and not document["implementation_refs"]
        ):
            raise DocumentationError(
                f"{document_id} with delivery {document['delivery']} must reference implementation"
            )
        if document["delivery"] in EVIDENCE_STATES and not document["evidence"]:
            raise DocumentationError(
                f"{document_id} with delivery {document['delivery']} must reference evidence"
            )
        for evidence_path in document["evidence"]:
            if not _is_normalized_relative_path(evidence_path):
                raise DocumentationError(
                    f"{document_id} evidence must be a normalized repository-relative path: "
                    f"{evidence_path}"
                )
            if not _repository_path(root, evidence_path).is_file():
                raise DocumentationError(
                    f"{document_id} evidence does not exist: {evidence_path}"
                )
            _assert_repository_containment(
                root,
                evidence_path,
                label=f"{document_id} evidence",
            )
    return documents


def _is_allowed_root_markdown(path):
    name = path.name
    return name in {"README.md", "CHANGELOG.md", "AGENTS.md"} or name.startswith(
        ("LICENSE", "NOTICE")
    )


def _is_allowed_github_markdown(path):
    relative = path.as_posix()
    if relative == ".github/pull_request_template.md":
        return True
    return len(path.parts) >= 3 and path.parts[1] in {
        "ISSUE_TEMPLATE",
        "PULL_REQUEST_TEMPLATE",
    }


def _markdown_inventory(root):
    inventory = set()
    candidates = []
    for path in root.rglob("*.md"):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRECTORY_NAMES for part in relative.parts):
            continue
        candidates.append((relative.as_posix(), relative))
    for relative_text, relative in sorted(candidates):
        if relative.parts[0] == ".github":
            if _is_allowed_github_markdown(relative):
                continue
            raise DocumentationError(
                f"Markdown document in .github must be a platform template: {relative_text}"
            )
        if relative.parts[0] == "docs":
            inventory.add(relative_text)
            continue
        if len(relative.parts) == 1 and _is_allowed_root_markdown(relative):
            if relative.name != "AGENTS.md":
                inventory.add(relative_text)
            continue
        raise DocumentationError(f"Markdown document must live under docs/: {relative_text}")
    return inventory


def _validate_markdown_inventory(root, documents):
    inventory = _markdown_inventory(root)
    registered = {document["path"] for document in documents}
    unregistered = sorted(inventory - registered)
    if unregistered:
        raise DocumentationError(f"unregistered Markdown document: {unregistered[0]}")
    unmanaged = sorted(registered - inventory)
    if unmanaged:
        raise DocumentationError(
            f"registered document is outside the managed Markdown inventory: {unmanaged[0]}"
        )


def _validate_relationships(documents):
    by_id = {document["id"]: document for document in documents}
    replacement_targets = set()
    for document in documents:
        for superseded_id in document["supersedes"]:
            if superseded_id not in by_id:
                raise DocumentationError(
                    f"{document['id']} supersedes unknown document: {superseded_id}"
                )
            if superseded_id == document["id"]:
                raise DocumentationError(f"{document['id']} cannot supersede itself")
            target = by_id[superseded_id]
            if target["lifecycle"] != "superseded":
                raise DocumentationError(
                    f"{document['id']} supersedes {superseded_id} but its lifecycle is "
                    f"{target['lifecycle']}"
                )
            replacement_targets.add(superseded_id)

    states = {}
    stack = []

    def visit(document_id):
        state = states.get(document_id, 0)
        if state == 2:
            return
        if state == 1:
            cycle_start = stack.index(document_id)
            cycle = stack[cycle_start:] + [document_id]
            raise DocumentationError(
                f"supersedes relationship contains a cycle: {' -> '.join(cycle)}"
            )
        states[document_id] = 1
        stack.append(document_id)
        for superseded_id in by_id[document_id]["supersedes"]:
            visit(superseded_id)
        stack.pop()
        states[document_id] = 2

    for document in documents:
        visit(document["id"])

    for document in documents:
        if (
            document["lifecycle"] == "superseded"
            and document["id"] not in replacement_targets
        ):
            raise DocumentationError(
                f"superseded document has no replacement: {document['id']}"
            )

    registered_paths = {document["path"]: document for document in documents}
    versions = sorted(
        {version for document in documents for version in document["releases"]}
    )
    for version in versions:
        manifest_path = f"docs/releases/v{version}.md"
        manifest = registered_paths.get(manifest_path)
        if manifest is None:
            raise DocumentationError(
                f"release {version} is referenced but {manifest_path} is not registered"
            )
        if manifest["kind"] != "release":
            raise DocumentationError(f"{manifest_path} must use kind release")
    return versions


def _local_links(source_path, content):
    base = posixpath.dirname(source_path)
    links = set()
    for match in LINK_PATTERN.finditer(content):
        target = match.group("target").strip("<>")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.partition("#")[0]
        if not target:
            continue
        links.add(posixpath.normpath(posixpath.join(base, target)))
    return links


def _production_deployment_status(manifest_path, content):
    matches = re.findall(
        r"(?mi)^\s*(?:[-*+]\s+)?生产部署\s*[：:]\s*(?P<value>.+?)\s*$",
        content,
    )
    if len(matches) != 1:
        raise DocumentationError(
            f"{manifest_path} must state production deployment status exactly once"
        )
    value = re.sub(r"[`*_~]", "", matches[0]).strip()
    match = re.match(r"(?i)(PASS|BLOCKED)(?=$|[^A-Za-z0-9_])", value)
    if match is None:
        raise DocumentationError(
            f"{manifest_path} production deployment status must be PASS or BLOCKED"
        )
    return match.group(1).upper()


def _validate_release_manifests(root, versions, documents):
    gate_content = _read_text(root, RELEASE_GATE_PATH)
    for version in versions:
        manifest_path = f"docs/releases/v{version}.md"
        content = _read_text(root, manifest_path)
        if not content.startswith(f"# v{version}\n"):
            raise DocumentationError(f"{manifest_path} must start with # v{version}")
        for heading in (
            "## 版本身份",
            "## 变更范围",
            "## 关联文档",
            "## 验证与部署结论",
        ):
            if heading not in content:
                raise DocumentationError(f"{manifest_path} is missing heading: {heading}")
        links = _local_links(manifest_path, content)
        if "CHANGELOG.md" not in links:
            raise DocumentationError(f"{manifest_path} must link CHANGELOG.md")
        if RELEASE_GATE_PATH not in links:
            raise DocumentationError(f"{manifest_path} must link {RELEASE_GATE_PATH}")
        expected_paths = sorted(
            document["path"]
            for document in documents
            if version in document["releases"] and document["path"] != manifest_path
        )
        for expected_path in expected_paths:
            if expected_path not in links:
                raise DocumentationError(
                    f"{manifest_path} does not link registered release document: "
                    f"{expected_path}"
                )
        if "源代码发布：" not in content:
            raise DocumentationError(f"{manifest_path} must state source release status")
        production_status = _production_deployment_status(manifest_path, content)
        if "BLOCKED" in gate_content and production_status == "PASS":
            raise DocumentationError(
                f"{manifest_path} cannot mark production PASS while the release gate is BLOCKED"
            )


def _validate_index(root, documents):
    content = _read_text(root, INDEX_PATH)
    links = _local_links(INDEX_PATH, content)
    if REGISTRY_PATH not in links:
        raise DocumentationError(f"{INDEX_PATH} must link {REGISTRY_PATH}")
    for path in sorted(document["path"] for document in documents):
        if path == INDEX_PATH:
            continue
        if path not in links:
            raise DocumentationError(
                f"{INDEX_PATH} does not link registered document: {path}"
            )


def _validate_integrations(root):
    readme = _read_text(root, "README.md")
    if INDEX_PATH not in _local_links("README.md", readme):
        raise DocumentationError("README.md must link docs/README.md")
    if DOCUMENTATION_COMMAND not in readme:
        raise DocumentationError(f"README.md must run {DOCUMENTATION_COMMAND}")

    agents = _read_text(root, "AGENTS.md")
    if POLICY_PATH not in agents:
        raise DocumentationError(f"AGENTS.md must reference {POLICY_PATH}")
    if DOCUMENTATION_COMMAND not in agents:
        raise DocumentationError(f"AGENTS.md must run {DOCUMENTATION_COMMAND}")

    workflow_path = ".github/workflows/ci.yml"
    workflow = _read_text(root, workflow_path)
    if DOCUMENTATION_COMMAND not in workflow:
        raise DocumentationError(f"{workflow_path} must run {DOCUMENTATION_COMMAND}")


def verify(root):
    registry = _load_registry(root)
    documents = _validate_entries(root, registry)
    _validate_markdown_inventory(root, documents)
    versions = _validate_relationships(documents)
    _validate_release_manifests(root, versions, documents)
    _validate_index(root, documents)
    _validate_integrations(root)
    return len(documents)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify documentation governance contracts"
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        count = verify(args.root)
    except DocumentationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Documentation verified: {count} registered Markdown documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
