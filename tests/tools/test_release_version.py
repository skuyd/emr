from pathlib import Path
import json
import subprocess
import sys

import pytest

from tools import release_version


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "release_version.py"


def run_version_tool(repo, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def write_release_repo(repo, version="0.1.0"):
    (repo / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "example"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (repo / "package.json").write_text(
        json.dumps({"name": "example", "version": version}),
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": version,
                "lockfileVersion": 3,
                "packages": {"": {"name": "example", "version": version}},
            }
        ),
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        f"## [{version}] - 2026-09-03\n\n- Initial baseline.\n",
        encoding="utf-8",
    )


def test_show_prints_the_canonical_product_version(tmp_path):
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")

    result = run_version_tool(tmp_path, "show")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "0.1.0\n"


def test_show_hides_the_release_please_marker(tmp_path):
    (tmp_path / "VERSION").write_text(
        "0.1.0 # x-release-please-version\n",
        encoding="utf-8",
    )

    result = run_version_tool(tmp_path, "show")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "0.1.0\n"


def test_check_accepts_consistent_product_version_metadata(tmp_path):
    write_release_repo(tmp_path)

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Version metadata is consistent: 0.1.0\n"


def test_check_accepts_release_please_changelog_format(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "<!-- Release Please inserts generated entries below this heading. -->\n\n"
        "## [0.1.0](https://github.com/example/repo/compare/v0.0.0...v0.1.0) "
        "(2026-09-03)\n\n"
        "### 新增\n\n"
        "- 建立初始版本。\n",
        encoding="utf-8",
    )

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Version metadata is consistent: 0.1.0\n"


def test_check_reports_drift_from_the_canonical_version(tmp_path):
    write_release_repo(tmp_path)
    package_path = tmp_path / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["version"] = "9.9.9"
    package_path.write_text(json.dumps(package), encoding="utf-8")

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: package.json version 9.9.9 does not match VERSION 0.1.0\n"
    )
    assert "Traceback" not in result.stderr


def test_check_reports_release_please_manifest_drift(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / ".release-please-manifest.json").write_text(
        json.dumps({".": "9.9.9"}),
        encoding="utf-8",
    )

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: .release-please-manifest.json version 9.9.9 "
        "does not match VERSION 0.1.0\n"
    )


@pytest.mark.parametrize(
    ("version", "displayed_version"),
    [
        ("1.0", "1.0"),
        ("v1.0.0", "v1.0.0"),
        ("1.0.0.0", "1.0.0.0"),
        ("01.0.0", "01.0.0"),
        ("1٢.0.0", "'1\\u0662.0.0'"),
    ],
)
def test_check_rejects_noncanonical_semver(tmp_path, version, displayed_version):
    write_release_repo(tmp_path, version=version)

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: VERSION must use MAJOR.MINOR.PATCH SemVer "
        f"(found {displayed_version})\n"
    )


def test_prepare_archives_unreleased_changes_and_synchronizes_every_version(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Added\n\n"
        "- Added the release workflow.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )

    result = run_version_tool(
        tmp_path,
        "prepare",
        "0.2.0",
        "--date",
        "2026-09-04",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Prepared release 0.2.0 (2026-09-04)\n"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8") == "0.2.0\n"
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert pyproject == '[project]\nname = "example"\nversion = "0.2.0"\n'
    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((tmp_path / "package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == "0.2.0"
    assert package_lock["version"] == "0.2.0"
    assert package_lock["packages"][""]["version"] == "0.2.0"
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "## [0.2.0] - 2026-09-04\n\n"
        "### Added\n\n"
        "- Added the release workflow.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n"
    )


def test_prepare_preserves_the_release_please_marker(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "VERSION").write_text(
        "0.1.0 # x-release-please-version\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )

    result = run_version_tool(
        tmp_path,
        "prepare",
        "0.2.0",
        "--date",
        "2026-09-04",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "VERSION").read_text(encoding="utf-8") == (
        "0.2.0 # x-release-please-version\n"
    )


def test_prepare_preserves_the_pyproject_release_please_marker(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\n'
        'version = "0.1.0" # x-release-please-version\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )

    result = run_version_tool(
        tmp_path,
        "prepare",
        "0.2.0",
        "--date",
        "2026-09-04",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == (
        '[project]\nname = "example"\n'
        'version = "0.2.0" # x-release-please-version\n'
    )


def test_prepare_supports_automatic_changelog_and_synchronizes_manifest(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "VERSION").write_text(
        "0.1.0 # x-release-please-version\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\n'
        'version = "0.1.0" # x-release-please-version\n',
        encoding="utf-8",
    )
    (tmp_path / ".release-please-manifest.json").write_text(
        json.dumps({".": "0.1.0"}),
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "### 修复\n\n"
        "- 修复自动发布流程。\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )

    result = run_version_tool(
        tmp_path,
        "prepare",
        "0.1.1",
        "--date",
        "2026-09-04",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(
        (tmp_path / ".release-please-manifest.json").read_text(encoding="utf-8")
    ) == {".": "0.1.1"}
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith(
        "# Changelog\n\n## Unreleased\n\n## [0.1.1] - 2026-09-04\n"
    )


@pytest.mark.parametrize("new_version", ["0.1.0", "0.0.9"])
def test_prepare_rejects_nonincreasing_versions_without_writing_files(
    tmp_path,
    new_version,
):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Fixed\n\n"
        "- Corrected a release issue.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}

    result = run_version_tool(
        tmp_path,
        "prepare",
        new_version,
        "--date",
        "2026-09-04",
    )

    assert result.returncode == 1
    assert result.stderr == (
        f"ERROR: release version {new_version} must be greater than current version 0.1.0\n"
    )
    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before


def test_prepare_rejects_an_empty_unreleased_section_without_writing_files(tmp_path):
    write_release_repo(tmp_path)
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}

    result = run_version_tool(
        tmp_path,
        "prepare",
        "0.2.0",
        "--date",
        "2026-09-04",
    )

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: CHANGELOG.md [Unreleased] must contain at least one bullet "
        "before preparing a release\n"
    )
    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before


def test_prepare_rejects_a_noncanonical_release_version_without_writing_files(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}

    result = run_version_tool(tmp_path, "prepare", "v0.2.0")

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: release version must use MAJOR.MINOR.PATCH SemVer (found v0.2.0)\n"
    )
    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before


def test_prepare_rejects_an_invalid_release_date_without_writing_files(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}

    result = run_version_tool(
        tmp_path,
        "prepare",
        "0.2.0",
        "--date",
        "2026-02-30",
    )

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: release date must use a real YYYY-MM-DD date (found 2026-02-30)\n"
    )
    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before


def test_check_requires_an_unreleased_changelog_section(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == "ERROR: CHANGELOG.md has no [Unreleased] section\n"


def test_check_rejects_multiple_unreleased_changelog_sections(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- First pending change.\n\n"
        "## [Unreleased]\n\n"
        "- Second pending change.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == (
        "ERROR: CHANGELOG.md must contain exactly one [Unreleased] section\n"
    )


def test_prepare_rejects_a_release_version_already_in_the_changelog(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added another release note.\n\n"
        "## [0.2.0] - 2026-09-04\n\n"
        "- Existing future release.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}

    result = run_version_tool(
        tmp_path,
        "prepare",
        "0.2.0",
        "--date",
        "2026-09-05",
    )

    assert result.returncode == 1
    assert result.stderr == "ERROR: CHANGELOG.md already has release entry for 0.2.0\n"
    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before


def test_check_rejects_duplicate_release_entries(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n\n"
        "## [0.1.0] - 2026-09-02\n\n"
        "- Duplicate baseline.\n",
        encoding="utf-8",
    )

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == "ERROR: CHANGELOG.md has duplicate release entry: 0.1.0\n"


def test_prepare_restores_every_file_when_an_atomic_replace_fails(
    tmp_path,
    monkeypatch,
):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}
    real_replace = Path.replace
    replacement_count = 0
    failure_injected = False

    def fail_the_second_replace(path, target):
        nonlocal replacement_count, failure_injected
        if not failure_injected and Path(target).name in tracked:
            replacement_count += 1
            if replacement_count == 2:
                failure_injected = True
                raise OSError("simulated replace failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_the_second_replace)

    with pytest.raises(
        release_version.VersionError,
        match="could not update release files; original files restored",
    ):
        release_version._prepare(tmp_path, "0.2.0", "2026-09-04")

    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(tracked)


def test_prepare_retains_the_backup_when_rollback_cannot_restore_a_file(
    tmp_path,
    monkeypatch,
):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}
    real_replace = Path.replace

    def fail_update_and_rollback(path, target):
        target = Path(target)
        if path.name.endswith(".new") and target.name == "package.json":
            raise OSError("simulated update failure")
        if path.name.endswith(".backup") and target.name == "pyproject.toml":
            raise OSError("simulated rollback failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_update_and_rollback)

    with pytest.raises(release_version.VersionError) as captured:
        release_version._prepare(tmp_path, "0.2.0", "2026-09-04")

    assert "rollback was incomplete" in str(captured.value)
    assert "recovery backup retained at" in str(captured.value)
    backups = list(tmp_path.glob(".pyproject.toml.release-*.backup"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before["pyproject.toml"]
    assert (tmp_path / "pyproject.toml").read_bytes() != before["pyproject.toml"]
    for name in tracked:
        if name != "pyproject.toml":
            assert (tmp_path / name).read_bytes() == before[name]
    release_temps = list(tmp_path.glob(".*.release-*"))
    assert release_temps == backups


def test_prepare_restores_every_file_when_replacement_is_interrupted(
    tmp_path,
    monkeypatch,
):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}
    real_replace = Path.replace
    replacement_count = 0

    def interrupt_the_second_replace(path, target):
        nonlocal replacement_count
        if path.name.endswith(".new") and Path(target).name in tracked:
            replacement_count += 1
            if replacement_count == 2:
                raise KeyboardInterrupt
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_the_second_replace)
    captured = None
    try:
        release_version._prepare(tmp_path, "0.2.0", "2026-09-04")
    except release_version.VersionError as error:
        captured = error
    except KeyboardInterrupt:
        pass

    assert captured is not None
    assert str(captured) == "release preparation interrupted; original files restored"
    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(tracked)


def test_prepare_rolls_back_when_interrupted_immediately_after_a_replace(
    tmp_path,
    monkeypatch,
):
    write_release_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "- Added release notes.\n\n"
        "## [0.1.0] - 2026-09-03\n\n"
        "- Initial baseline.\n",
        encoding="utf-8",
    )
    tracked = [
        "VERSION",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "CHANGELOG.md",
    ]
    before = {name: (tmp_path / name).read_bytes() for name in tracked}
    real_replace = Path.replace
    replacement_count = 0

    def interrupt_after_the_second_replace(path, target):
        nonlocal replacement_count
        if path.name.endswith(".new") and Path(target).name in tracked:
            replacement_count += 1
            if replacement_count == 2:
                real_replace(path, target)
                raise KeyboardInterrupt
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_after_the_second_replace)

    with pytest.raises(
        release_version.VersionError,
        match="release preparation interrupted; original files restored",
    ):
        release_version._prepare(tmp_path, "0.2.0", "2026-09-04")

    assert {name: (tmp_path / name).read_bytes() for name in tracked} == before
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(tracked)


def test_check_reports_a_missing_required_release_file_without_a_traceback(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "VERSION").unlink()

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == "ERROR: required release file is missing: VERSION\n"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("filename", "contents", "expected_error"),
    [
        (
            "pyproject.toml",
            "[project\n",
            "ERROR: pyproject.toml is not valid TOML\n",
        ),
        (
            "package.json",
            "{",
            "ERROR: package.json is not valid JSON\n",
        ),
        (
            "package-lock.json",
            json.dumps({"version": "0.1.0", "packages": {"": {}}}),
            'ERROR: package-lock.json is missing packages[""].version\n',
        ),
    ],
)
def test_check_reports_malformed_release_metadata_without_a_traceback(
    tmp_path,
    filename,
    contents,
    expected_error,
):
    write_release_repo(tmp_path)
    (tmp_path / filename).write_text(contents, encoding="utf-8")

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == expected_error
    assert "Traceback" not in result.stderr


def test_check_reports_non_utf8_release_metadata_without_a_traceback(tmp_path):
    write_release_repo(tmp_path)
    (tmp_path / "package.json").write_bytes(b"\xff")

    result = run_version_tool(tmp_path, "check")

    assert result.returncode == 1
    assert result.stderr == "ERROR: package.json is not valid UTF-8\n"
    assert "Traceback" not in result.stderr


def test_repository_release_metadata_is_consistent():
    result = run_version_tool(ROOT, "check")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Version metadata is consistent: 0.1.0\n"
