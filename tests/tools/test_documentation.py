from pathlib import Path
import json
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "verify_documentation.py"


def run_verifier(repo):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )


def write_text(repo, relative_path, content):
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_registry(repo):
    return json.loads((repo / "docs" / "document-registry.json").read_text(encoding="utf-8"))


def write_registry(repo, registry):
    write_text(
        repo,
        "docs/document-registry.json",
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
    )


def document_entry(
    document_id,
    path,
    *,
    kind="guide",
    lifecycle="active",
    delivery="not_applicable",
    releases=None,
    supersedes=None,
    implementation_refs=None,
    evidence=None,
):
    return {
        "id": document_id,
        "title": document_id.replace("-", " ").title(),
        "path": path,
        "kind": kind,
        "lifecycle": lifecycle,
        "delivery": delivery,
        "owner": "engineering",
        "releases": releases or [],
        "supersedes": supersedes or [],
        "implementation_refs": implementation_refs or [],
        "evidence": evidence or [],
    }


def write_documentation_repo(repo):
    documents = [
        document_entry("repo-readme", "README.md"),
        document_entry("changelog", "CHANGELOG.md", kind="release"),
        document_entry("document-index", "docs/README.md"),
        document_entry(
            "policy-governance",
            "docs/policies/document-governance.md",
            kind="policy",
        ),
        document_entry(
            "spec-example",
            "docs/specs/2026-09-04-example-design.md",
            kind="spec",
            delivery="verified",
            releases=["0.1.0"],
            implementation_refs=["commit:abcdef0"],
            evidence=["docs/verification/evidence.json"],
        ),
        document_entry(
            "evidence-release-gate",
            "docs/verification/release-gate.md",
            kind="evidence",
            delivery="verified",
            releases=["0.1.0"],
            evidence=["docs/verification/evidence.json"],
        ),
        document_entry(
            "release-v0-1-0",
            "docs/releases/v0.1.0.md",
            kind="release",
            delivery="verified",
            releases=["0.1.0"],
            implementation_refs=["commit:abcdef0"],
            evidence=["CHANGELOG.md"],
        ),
    ]
    write_text(
        repo,
        "README.md",
        "# Example\n\n[Documentation](docs/README.md)\n\n"
        "`python tools/verify_documentation.py`\n",
    )
    write_text(repo, "CHANGELOG.md", "# Changelog\n\n## [0.1.0]\n")
    write_text(
        repo,
        "AGENTS.md",
        "# Rules\n\nRead `docs/policies/document-governance.md` and run "
        "`python tools/verify_documentation.py`.\n",
    )
    write_text(
        repo,
        ".github/workflows/ci.yml",
        "name: CI\njobs:\n  test:\n    steps:\n"
        "      - run: python tools/verify_documentation.py\n",
    )
    write_text(
        repo,
        "docs/README.md",
        "# Documentation\n\n"
        "[Registry](document-registry.json)\n"
        "[Policy](policies/document-governance.md)\n"
        "[Repository](../README.md)\n"
        "[Changelog](../CHANGELOG.md)\n"
        "[Spec](specs/2026-09-04-example-design.md)\n"
        "[Evidence](verification/release-gate.md)\n"
        "[Release](releases/v0.1.0.md)\n",
    )
    write_text(
        repo,
        "docs/policies/document-governance.md",
        "# Document governance\n",
    )
    write_text(repo, "docs/specs/2026-09-04-example-design.md", "# Design\n")
    write_text(
        repo,
        "docs/verification/release-gate.md",
        "# Release gate\n\n**结论：BLOCKED**\n",
    )
    write_text(repo, "docs/verification/evidence.json", "{}\n")
    write_text(
        repo,
        "docs/releases/v0.1.0.md",
        "# v0.1.0\n\n"
        "## 版本身份\n\n[Changelog](../../CHANGELOG.md)\n\n"
        "## 变更范围\n\nInitial.\n\n"
        "## 关联文档\n\n[Spec](../specs/2026-09-04-example-design.md)\n\n"
        "## 验证与部署结论\n\n"
        "源代码发布：已发布\n\n"
        "生产部署：`BLOCKED`\n\n"
        "[Release gate](../verification/release-gate.md)\n",
    )
    write_text(
        repo,
        "docs/document-registry.json",
        json.dumps(
            {"schema_version": 1, "documents": documents},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def test_complete_documentation_repository_is_accepted(tmp_path):
    write_documentation_repo(tmp_path)

    result = run_verifier(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Documentation verified: 7 registered Markdown documents\n"


def test_verifier_rejects_an_unregistered_markdown_file(tmp_path):
    write_documentation_repo(tmp_path)
    write_text(tmp_path, "docs/orphan.md", "# Orphan\n")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "ERROR: unregistered Markdown document: docs/orphan.md\n"


def test_verifier_rejects_an_unsupported_registry_schema(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["schema_version"] = 2
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: document registry schema_version must be 1\n"


def test_verifier_rejects_a_duplicate_document_path(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][1]["path"] = "README.md"
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: duplicate document path: README.md\n"


def test_verifier_rejects_a_missing_required_entry_field(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    del registry["documents"][0]["owner"]
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: repo-readme is missing required field: owner\n"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "memo"),
        ("lifecycle", "current"),
        ("delivery", "done"),
        ("owner", "team"),
    ],
)
def test_verifier_rejects_an_invalid_registry_enum(tmp_path, field, value):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][0][field] = value
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == f"ERROR: repo-readme has invalid {field}: {value}\n"


def test_verifier_rejects_a_document_kind_in_the_wrong_directory(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["kind"] = "product"
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: spec-example with kind product must live under docs/product/\n"
    )


def test_verifier_rejects_a_nonstandard_specification_filename(tmp_path):
    write_documentation_repo(tmp_path)
    old_path = "docs/specs/2026-09-04-example-design.md"
    new_path = "docs/specs/final-design.md"
    (tmp_path / old_path).rename(tmp_path / new_path)
    registry = read_registry(tmp_path)
    specification = next(
        document for document in registry["documents"] if document["id"] == "spec-example"
    )
    specification["path"] = new_path
    write_registry(tmp_path, registry)
    index = tmp_path / "docs" / "README.md"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            old_path.removeprefix("docs/"),
            new_path.removeprefix("docs/"),
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "docs" / "releases" / "v0.1.0.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "../specs/2026-09-04-example-design.md",
            "../specs/final-design.md",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: spec-example must use a YYYY-MM-DD-kebab-case.md filename\n"
    )


def test_verifier_rejects_a_path_that_escapes_the_repository(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][0]["path"] = "../README.md"
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: repo-readme path must be a normalized repository-relative path\n"


def test_verifier_rejects_a_registered_document_that_does_not_exist(tmp_path):
    write_documentation_repo(tmp_path)
    (tmp_path / "docs" / "specs" / "2026-09-04-example-design.md").unlink()

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: registered Markdown document does not exist: "
        "docs/specs/2026-09-04-example-design.md\n"
    )


def test_verifier_requires_evidence_for_a_verified_specification(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    specification = next(
        document for document in registry["documents"] if document["id"] == "spec-example"
    )
    specification["evidence"] = []
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: spec-example with delivery verified must reference evidence\n"


def test_verifier_requires_an_implementation_reference_for_a_verified_specification(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["implementation_refs"] = []
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: spec-example with delivery verified must reference implementation\n"
    )


def test_verifier_rejects_a_broken_evidence_path(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["evidence"] = ["docs/verification/missing.json"]
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: spec-example evidence does not exist: docs/verification/missing.json\n"
    )


def test_verifier_rejects_a_document_symlink_that_escapes_the_repository(tmp_path):
    write_documentation_repo(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}-external-spec.md"
    external.write_text("# External specification\n", encoding="utf-8")
    document = tmp_path / "docs" / "specs" / "2026-09-04-example-design.md"
    document.unlink()
    try:
        document.symlink_to(external)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: registered document resolves outside repository: "
        "docs/specs/2026-09-04-example-design.md\n"
    )


def test_verifier_rejects_an_evidence_symlink_that_escapes_the_repository(tmp_path):
    write_documentation_repo(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}-external-evidence.json"
    external.write_text("{}\n", encoding="utf-8")
    evidence = tmp_path / "docs" / "verification" / "evidence.json"
    evidence.unlink()
    try:
        evidence.symlink_to(external)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: spec-example evidence resolves outside repository: "
        "docs/verification/evidence.json\n"
    )


def test_verifier_rejects_a_malformed_implementation_reference(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["implementation_refs"] = ["commit:xyz"]
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: spec-example has invalid implementation reference: commit:xyz\n"


def test_verifier_rejects_a_broken_supersedes_reference(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["supersedes"] = ["missing-spec"]
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: spec-example supersedes unknown document: missing-spec\n"


def test_verifier_requires_a_superseded_lifecycle_for_a_replacement_target(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["supersedes"] = ["policy-governance"]
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: spec-example supersedes policy-governance but its lifecycle is active\n"
    )


def test_verifier_rejects_a_supersedes_cycle(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    specification = registry["documents"][4]
    policy = registry["documents"][3]
    specification["lifecycle"] = "superseded"
    specification["supersedes"] = ["policy-governance"]
    policy["lifecycle"] = "superseded"
    policy["supersedes"] = ["spec-example"]
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: supersedes relationship contains a cycle: "
        "policy-governance -> spec-example -> policy-governance\n"
    )


def test_verifier_requires_a_replacement_for_a_superseded_document(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["lifecycle"] = "superseded"
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: superseded document has no replacement: spec-example\n"


def test_verifier_requires_a_manifest_for_each_referenced_release(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"] = [
        document
        for document in registry["documents"]
        if document["id"] != "release-v0-1-0"
    ]
    write_registry(tmp_path, registry)
    (tmp_path / "docs" / "releases" / "v0.1.0.md").unlink()

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: release 0.1.0 is referenced but docs/releases/v0.1.0.md is not registered\n"
    )


def test_verifier_rejects_a_non_semver_release_reference(tmp_path):
    write_documentation_repo(tmp_path)
    registry = read_registry(tmp_path)
    registry["documents"][4]["releases"] = ["next"]
    write_registry(tmp_path, registry)

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: spec-example has invalid release: next\n"


def test_verifier_rejects_a_release_manifest_without_the_release_gate(tmp_path):
    write_documentation_repo(tmp_path)
    manifest = tmp_path / "docs" / "releases" / "v0.1.0.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[Release gate](../verification/release-gate.md)\n",
            "",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: docs/releases/v0.1.0.md must link docs/verification/release-gate.md\n"
    )


def test_verifier_requires_a_manifest_to_link_every_document_in_the_release(tmp_path):
    write_documentation_repo(tmp_path)
    manifest = tmp_path / "docs" / "releases" / "v0.1.0.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[Spec](../specs/2026-09-04-example-design.md)\n",
            "",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: docs/releases/v0.1.0.md does not link registered release document: "
        "docs/specs/2026-09-04-example-design.md\n"
    )


@pytest.mark.parametrize(
    "production_status",
    ["**PASS**", "`pass`", "**PASS**（生产批准）"],
)
def test_verifier_rejects_production_approval_while_the_gate_is_blocked(
    tmp_path,
    production_status,
):
    write_documentation_repo(tmp_path)
    manifest = tmp_path / "docs" / "releases" / "v0.1.0.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "生产部署：`BLOCKED`",
            f"生产部署：{production_status}",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: docs/releases/v0.1.0.md cannot mark production PASS while the release gate is BLOCKED\n"
    )


def test_verifier_requires_the_index_to_link_every_registered_document(tmp_path):
    write_documentation_repo(tmp_path)
    index = tmp_path / "docs" / "README.md"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "[Spec](specs/2026-09-04-example-design.md)\n",
            "",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: docs/README.md does not link registered document: "
        "docs/specs/2026-09-04-example-design.md\n"
    )


def test_verifier_requires_the_index_to_link_the_registry(tmp_path):
    write_documentation_repo(tmp_path)
    index = tmp_path / "docs" / "README.md"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "[Registry](document-registry.json)\n",
            "",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: docs/README.md must link docs/document-registry.json\n"


def test_verifier_rejects_markdown_in_a_source_or_deployment_directory(tmp_path):
    write_documentation_repo(tmp_path)
    write_text(tmp_path, "deploy/notes.md", "# Notes\n")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: Markdown document must live under docs/: deploy/notes.md\n"


def test_verifier_ignores_generated_markdown_in_the_runtime_directory(tmp_path):
    write_documentation_repo(tmp_path)
    write_text(
        tmp_path,
        ".runtime/vendor/LICENSE.md",
        "# Vendored runtime license\n",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Documentation verified: 7 registered Markdown documents\n"


def test_verifier_ignores_markdown_excluded_by_the_repository(tmp_path):
    write_documentation_repo(tmp_path)
    write_text(tmp_path, ".gitignore", "/generated/\n")
    write_text(tmp_path, "generated/tool-progress.md", "# Tool progress\n")
    initialized = subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr

    result = run_verifier(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Documentation verified: 7 registered Markdown documents\n"


def test_verifier_rejects_non_template_markdown_under_github(tmp_path):
    write_documentation_repo(tmp_path)
    write_text(tmp_path, ".github/design-notes.md", "# Design notes\n")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: Markdown document in .github must be a platform template: "
        ".github/design-notes.md\n"
    )


def test_verifier_requires_the_root_readme_documentation_entry(tmp_path):
    write_documentation_repo(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("docs/README.md", "docs/missing.md"),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == "ERROR: README.md must link docs/README.md\n"


def test_verifier_requires_the_root_readme_to_document_the_check(tmp_path):
    write_documentation_repo(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "python tools/verify_documentation.py",
            "python tools/other_check.py",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: README.md must run python tools/verify_documentation.py\n"
    )


def test_verifier_requires_agents_to_reference_the_policy_and_command(tmp_path):
    write_documentation_repo(tmp_path)
    write_text(tmp_path, "AGENTS.md", "# Rules\n")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: AGENTS.md must reference docs/policies/document-governance.md\n"
    )


def test_verifier_requires_agents_to_run_the_documentation_check(tmp_path):
    write_documentation_repo(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(
            "python tools/verify_documentation.py",
            "python tools/other_check.py",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: AGENTS.md must run python tools/verify_documentation.py\n"
    )


def test_verifier_requires_ci_to_run_the_documentation_check(tmp_path):
    write_documentation_repo(tmp_path)
    write_text(tmp_path, ".github/workflows/ci.yml", "name: CI\n")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: .github/workflows/ci.yml must run python tools/verify_documentation.py\n"
    )


def test_repository_documentation_is_consistent():
    result = run_verifier(ROOT)

    assert result.returncode == 0, result.stderr
    count = len(json.loads((ROOT / "docs/document-registry.json").read_text(encoding="utf-8"))["documents"])
    assert result.stdout == f"Documentation verified: {count} registered Markdown documents\n"
