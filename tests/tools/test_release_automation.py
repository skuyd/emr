from pathlib import Path
import json
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "verify_release_automation.py"
REPOSITORY_VERSION = (
    (ROOT / "VERSION").read_text(encoding="utf-8").partition("#")[0].strip()
)
PIN = "a" * 40
RELEASE_PLEASE_COMMIT = "45996ed1f6d02564a971a2fa1b5860e934307cf7"
RELEASE_PLEASE_TAG_OBJECT = "0dfd8538845b8e92600d271a895a5372865d4062"


def run_verifier(repo):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )


def write_automation_repo(repo):
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "VERSION").write_text(
        "0.1.0 # x-release-please-version\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "example"\n'
        'version = "0.1.0" # x-release-please-version\n\n'
        '[project.optional-dependencies]\ntest = ["PyYAML>=6.0,<7"]\n',
        encoding="utf-8",
    )
    (repo / "package.json").write_text(
        json.dumps({"name": "example", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "0.1.0",
                "lockfileVersion": 3,
                "packages": {"": {"name": "example", "version": "0.1.0"}},
            }
        ),
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n"
        "## [0.1.0] - 2026-09-03\n\n- Initial baseline.\n",
        encoding="utf-8",
    )
    (repo / ".release-please-manifest.json").write_text(
        json.dumps({".": "0.1.0"}),
        encoding="utf-8",
    )
    (repo / "release-please-config.json").write_text(
        json.dumps(
            {
                "bootstrap-sha": "b" * 40,
                "packages": {
                    ".": {
                        "release-type": "node",
                        "package-name": "family-phr",
                        "include-v-in-tag": True,
                        "include-component-in-tag": False,
                        "versioning-strategy": "default",
                        "bump-minor-pre-major": False,
                        "bump-patch-for-minor-pre-major": False,
                        "pull-request-title-pattern": "chore: 发布 ${version}",
                        "extra-files": [
                            {"type": "generic", "path": "VERSION"},
                            {"type": "generic", "path": "pyproject.toml"},
                        ],
                        "changelog-sections": [
                            {"type": "feat", "section": "新增"},
                            {"type": "fix", "section": "修复"},
                            {"type": "perf", "section": "性能"},
                            {"type": "docs", "section": "文档", "hidden": True},
                            {"type": "test", "section": "测试", "hidden": True},
                            {"type": "chore", "section": "维护", "hidden": True},
                            {"type": "ci", "section": "持续集成", "hidden": True},
                            {
                                "type": "refactor",
                                "section": "重构",
                                "hidden": True,
                            },
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        f"""name: CI
"on":
  pull_request:
    types: [opened, edited, synchronize, reopened]
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  conventional-title:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{PIN}
      - uses: actions/setup-python@{PIN}
        with:
          python-version: "3.11"
      - env:
          PR_TITLE: ${{{{ github.event.pull_request.title }}}}
          PR_BODY: ${{{{ github.event.pull_request.body }}}}
        run: python tools/check_conventional_commit.py "$PR_TITLE" --body "$PR_BODY"
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{PIN}
      - uses: actions/setup-python@{PIN}
        with:
          python-version: "3.11"
      - uses: actions/setup-node@{PIN}
        with:
          node-version: "22"
      - run: python -m pip install -e ".[test]"
      - run: npm ci
      - run: python tools/verify_release_automation.py
      - run: python tools/release_version.py check
      - run: python -m pytest -q
      - run: npm run test:js
""",
        encoding="utf-8",
    )
    (repo / ".github" / "workflows" / "release.yml").write_text(
        f"""name: Automatic release
"on":
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: write
  issues: write
  pull-requests: write
concurrency:
  group: automatic-release
  cancel-in-progress: false
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - id: release
        uses: googleapis/release-please-action@{RELEASE_PLEASE_COMMIT}
        with:
          token: ${{{{ secrets.RELEASE_PLEASE_TOKEN }}}}
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json
      - if: ${{{{ steps.release.outputs.prs_created == 'true' }}}}
        env:
          GH_TOKEN: ${{{{ secrets.RELEASE_PLEASE_TOKEN }}}}
          RELEASE_PR: ${{{{ steps.release.outputs.pr }}}}
        run: |
          pr_number="$(jq -r '.number' <<<"$RELEASE_PR")"
          gh pr merge "$pr_number" --repo "$GITHUB_REPOSITORY" --squash --auto
""",
        encoding="utf-8",
    )


def test_complete_automatic_release_configuration_is_accepted(tmp_path):
    write_automation_repo(tmp_path)

    result = run_verifier(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Release automation verified: 0.1.0, target=main, mode=automatic\n"
    )


def test_verifier_requires_the_release_please_version_marker(tmp_path):
    write_automation_repo(tmp_path)
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: VERSION must contain the x-release-please-version marker\n"
    )


def test_verifier_rejects_manifest_version_drift(tmp_path):
    write_automation_repo(tmp_path)
    (tmp_path / ".release-please-manifest.json").write_text(
        json.dumps({".": "9.9.9"}),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: .release-please-manifest.json version 9.9.9 does not match VERSION 0.1.0\n"
    )


def test_verifier_rejects_mutable_action_references(tmp_path):
    write_automation_repo(tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "release.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            f"googleapis/release-please-action@{RELEASE_PLEASE_COMMIT}",
            "googleapis/release-please-action@v5",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: .github/workflows/release.yml action references must be pinned to 40-character commits: "
        "googleapis/release-please-action@v5\n"
    )


def test_verifier_requires_the_reviewed_release_please_commit(tmp_path):
    write_automation_repo(tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "release.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            RELEASE_PLEASE_COMMIT,
            RELEASE_PLEASE_TAG_OBJECT,
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: .github/workflows/release.yml must pin release-please to the reviewed "
        f"commit {RELEASE_PLEASE_COMMIT}\n"
    )


def test_verifier_requires_pyyaml_in_test_dependencies(tmp_path):
    write_automation_repo(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            'test = ["PyYAML>=6.0,<7"]',
            "test = []",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: pyproject.toml test dependencies must include PyYAML>=6.0,<7\n"
    )


def test_verifier_rejects_direct_pr_title_shell_interpolation(tmp_path):
    write_automation_repo(tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            '"$PR_TITLE"',
            '"${{ github.event.pull_request.title }}"',
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: .github/workflows/ci.yml must pass untrusted PR text through environment variables\n"
    )


def test_verifier_requires_ci_to_rerun_when_a_pr_title_is_edited(tmp_path):
    write_automation_repo(tmp_path)
    workflow_path = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            "types: [opened, edited, synchronize, reopened]",
            "types: [opened, synchronize, reopened]",
        ),
        encoding="utf-8",
    )

    result = run_verifier(tmp_path)

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: .github/workflows/ci.yml must rerun when pull request titles are edited\n"
    )


def test_repository_release_automation_is_consistent():
    result = run_verifier(ROOT)

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"Release automation verified: {REPOSITORY_VERSION}, target=main, mode=automatic\n"
    )


def test_repository_documents_strict_release_pr_checks():
    instructions = (ROOT / "docs" / "versioning.md").read_text(encoding="utf-8")

    assert "Require branches to be up to date before merging" in instructions
    assert "过期的发布 PR" in instructions
