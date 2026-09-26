from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tomllib

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by installation, not unit tests
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
SEMVER_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
ACTION_PIN_PATTERN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
RELEASE_PLEASE_VERSION = "17.6.0"
VERSION_MARKER = "# x-release-please-version"
RELEASING_SECTIONS = {"feat": "新增", "fix": "修复", "perf": "性能"}
HIDDEN_SECTIONS = {"docs", "test", "chore", "ci", "refactor"}


class AutomationError(ValueError):
    pass


def _read_text(root, name):
    try:
        return (root / name).read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise AutomationError(f"required automation file is missing: {name}") from error
    except UnicodeDecodeError as error:
        raise AutomationError(f"{name} is not valid UTF-8") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise AutomationError(f"could not read {name}: {detail}") from error


def _load_json(root, name):
    try:
        return json.loads(_read_text(root, name))
    except json.JSONDecodeError as error:
        raise AutomationError(f"{name} is not valid JSON") from error


def _load_toml(root, name):
    try:
        return tomllib.loads(_read_text(root, name))
    except tomllib.TOMLDecodeError as error:
        raise AutomationError(f"{name} is not valid TOML") from error


def _load_workflow(root, name):
    if yaml is None:
        raise AutomationError(
            "PyYAML is required; install project test dependencies with "
            "python -m pip install -e '.[test]'"
        )
    try:
        value = yaml.safe_load(_read_text(root, name))
    except yaml.YAMLError as error:
        raise AutomationError(f"{name} is not valid YAML") from error
    if not isinstance(value, dict):
        raise AutomationError(f"{name} must contain a workflow object")
    return value


def _canonical_version(root):
    source = _read_text(root, "VERSION").strip()
    if VERSION_MARKER not in source:
        raise AutomationError("VERSION must contain the x-release-please-version marker")
    match = re.fullmatch(
        rf"(?P<version>{SEMVER_PATTERN.pattern})\s+# x-release-please-version",
        source,
    )
    if match is None:
        raise AutomationError(
            "VERSION must contain only MAJOR.MINOR.PATCH and the release-please marker"
        )
    return match.group("version")


def _metadata_versions(root):
    pyproject = _load_toml(root, "pyproject.toml")
    package = _load_json(root, "package.json")
    package_lock = _load_json(root, "package-lock.json")
    try:
        return {
            "pyproject.toml": pyproject["project"]["version"],
            "package.json": package["version"],
            "package-lock.json": package_lock["version"],
            'package-lock.json packages[""]': package_lock["packages"][""]["version"],
        }
    except (KeyError, TypeError) as error:
        raise AutomationError("release metadata is missing a required version field") from error


def _validate_versions(root, version):
    for name, value in _metadata_versions(root).items():
        if value != version:
            raise AutomationError(
                f"{name} version {value} does not match VERSION {version}"
            )
    manifest = _load_json(root, ".release-please-manifest.json")
    manifest_version = manifest.get(".") if isinstance(manifest, dict) else None
    if manifest_version != version:
        raise AutomationError(
            ".release-please-manifest.json version "
            f"{manifest_version} does not match VERSION {version}"
        )


def _validate_tool_dependencies(root):
    pyproject = _load_toml(root, "pyproject.toml")
    pyproject_source = _read_text(root, "pyproject.toml")
    if re.search(
        r'(?m)^version\s*=\s*"[^\r\n"]+"\s+# x-release-please-version\s*$',
        pyproject_source,
    ) is None:
        raise AutomationError(
            "pyproject.toml project version must contain the x-release-please-version marker"
        )
    try:
        test_dependencies = pyproject["project"]["optional-dependencies"]["test"]
    except (KeyError, TypeError) as error:
        raise AutomationError(
            "pyproject.toml test dependencies must include PyYAML>=6.0,<7"
        ) from error
    if "PyYAML>=6.0,<7" not in test_dependencies:
        raise AutomationError(
            "pyproject.toml test dependencies must include PyYAML>=6.0,<7"
        )


def _validate_release_config(root):
    config = _load_json(root, "release-please-config.json")
    if config.get("always-update") is not True:
        raise AutomationError("release-please-config.json must set always-update to refresh existing candidates")
    try:
        package = config["packages"]["."]
    except (KeyError, TypeError) as error:
        raise AutomationError(
            "release-please-config.json must configure the root package"
        ) from error

    expected = {
        "release-type": "node",
        "package-name": "family-phr",
        "include-v-in-tag": True,
        "include-component-in-tag": False,
        "versioning-strategy": "default",
        "bump-minor-pre-major": False,
        "bump-patch-for-minor-pre-major": False,
        "pull-request-title-pattern": "chore: 发布 ${version}",
    }
    for key, value in expected.items():
        if package.get(key) != value:
            raise AutomationError(
                f"release-please-config.json root package must set {key} to {value!r}"
            )

    bootstrap_sha = config.get("bootstrap-sha")
    if not isinstance(bootstrap_sha, str) or re.fullmatch(r"[0-9a-f]{40}", bootstrap_sha) is None:
        raise AutomationError("release-please-config.json bootstrap-sha must be a full commit SHA")

    extra_files = package.get("extra-files")
    required_extra_files = [
        {"type": "generic", "path": "VERSION"},
        {"type": "generic", "path": "pyproject.toml"},
    ]
    if not isinstance(extra_files, list) or any(
        item not in extra_files for item in required_extra_files
    ):
        raise AutomationError(
            "release-please-config.json must synchronize VERSION and pyproject.toml"
        )

    sections = package.get("changelog-sections")
    if not isinstance(sections, list):
        raise AutomationError("release-please-config.json must define changelog-sections")
    by_type = {
        section.get("type"): section
        for section in sections
        if isinstance(section, dict) and isinstance(section.get("type"), str)
    }
    for commit_type, heading in RELEASING_SECTIONS.items():
        section = by_type.get(commit_type)
        if section is None or section.get("section") != heading or section.get("hidden") is True:
            raise AutomationError(
                f"release-please-config.json must publish {commit_type} changes under {heading}"
            )
    for commit_type in HIDDEN_SECTIONS:
        if by_type.get(commit_type, {}).get("hidden") is not True:
            raise AutomationError(
                f"release-please-config.json must hide {commit_type} changelog entries"
            )


def _workflow_trigger(workflow):
    return workflow.get("on", workflow.get(True, {}))


def _steps(workflow):
    jobs = workflow.get("jobs", {})
    for job in jobs.values() if isinstance(jobs, dict) else ():
        if isinstance(job, dict):
            for step in job.get("steps", ()):
                if isinstance(step, dict):
                    yield step


def _validate_pins(name, workflow):
    for step in _steps(workflow):
        reference = step.get("uses")
        if isinstance(reference, str) and not reference.startswith("./"):
            if ACTION_PIN_PATTERN.fullmatch(reference) is None:
                raise AutomationError(
                    f"{name} action references must be pinned to 40-character commits: "
                    f"{reference}"
                )


def _validate_ci_workflow(root):
    name = ".github/workflows/ci.yml"
    workflow = _load_workflow(root, name)
    _validate_pins(name, workflow)
    trigger = _workflow_trigger(workflow)
    if not isinstance(trigger, dict) or set(trigger) != {"workflow_dispatch"}:
        raise AutomationError(f"{name} must only use workflow_dispatch")
    runs = "\n".join(
        str(step.get("run", "")) for step in _steps(workflow) if step.get("run")
    )
    if (
        "${{ github.event.pull_request.title }}" in runs
        or "${{ github.event.pull_request.body }}" in runs
    ):
        raise AutomationError(
            f"{name} must pass untrusted PR text through environment variables"
        )
    for command in (
        "tools/check_conventional_commit.py",
        "tools/verify_release_automation.py",
        "tools/release_version.py check",
        "tools/verify_documentation.py",
        "python -m pytest -q",
        "npm run test:js",
    ):
        if command not in runs:
            raise AutomationError(f"{name} must run {command}")


def _validate_local_release(root):
    if (root / ".github/workflows/release.yml").exists():
        raise AutomationError(".github/workflows/release.yml must be retired")
    for path in sorted((root / ".github/workflows").glob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        name = path.relative_to(root).as_posix()
        workflow = _load_workflow(root, name)
        trigger = _workflow_trigger(workflow)
        if not isinstance(trigger, dict) or set(trigger) != {"workflow_dispatch"}:
            raise AutomationError(f"{name} must only use workflow_dispatch")
        _validate_pins(name, workflow)
        if any("release-please" in str(step.get("uses", "")) for step in _steps(workflow)):
            raise AutomationError(f"{name} cloud release publisher must be retired")
    package = _load_json(root, "package.json")
    lock = _load_json(root, "package-lock.json")
    try:
        versions = [package["devDependencies"]["release-please"],
                    lock["packages"][""]["devDependencies"]["release-please"],
                    lock["packages"]["node_modules/release-please"]["version"]]
        integrity = lock["packages"]["node_modules/release-please"]["integrity"]
    except (KeyError, TypeError) as error:
        raise AutomationError("local release-please dependency and lock are required") from error
    if versions != [RELEASE_PLEASE_VERSION] * 3 or not str(integrity).startswith("sha512-"):
        raise AutomationError(f"release-please must be locked to {RELEASE_PLEASE_VERSION} with integrity")
    for name in ("submit.py", "submit_validation.py", "local_validation.py"):
        _read_text(root, f"tools/{name}")


def verify(root):
    version = _canonical_version(root)
    _validate_versions(root, version)
    _validate_tool_dependencies(root)
    _validate_release_config(root)
    _validate_ci_workflow(root)
    _validate_local_release(root)
    return version


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify local release configuration")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        version = verify(args.root)
    except AutomationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Release automation verified: {version}, target=main, mode=local")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
