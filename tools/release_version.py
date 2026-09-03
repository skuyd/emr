from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SEMVER_PATTERN = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
RELEASE_PLEASE_MARKER = " # x-release-please-version"


class VersionError(ValueError):
    pass


def _parse_version(value, label="VERSION"):
    match = SEMVER_PATTERN.fullmatch(value)
    if match is None:
        displayed_value = value if value.isascii() else ascii(value)
        raise VersionError(
            f"{label} must use MAJOR.MINOR.PATCH SemVer (found {displayed_value})"
        )
    return tuple(int(part) for part in match.groups())


def _validate_release_date(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError as error:
        raise VersionError(
            f"release date must use a real YYYY-MM-DD date (found {value})"
        ) from error


def _read_text(root, name):
    try:
        return (root / name).read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise VersionError(f"{name} is not valid UTF-8") from error
    except FileNotFoundError as error:
        raise VersionError(f"required release file is missing: {name}") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise VersionError(f"could not read required release file {name}: {detail}") from error


def _load_toml(root, name):
    try:
        return tomllib.loads(_read_text(root, name))
    except tomllib.TOMLDecodeError as error:
        raise VersionError(f"{name} is not valid TOML") from error


def _load_json(root, name):
    try:
        return json.loads(_read_text(root, name))
    except json.JSONDecodeError as error:
        raise VersionError(f"{name} is not valid JSON") from error


def _read_canonical_version(root):
    value = _read_text(root, "VERSION").strip()
    if value.endswith(RELEASE_PLEASE_MARKER):
        return value[: -len(RELEASE_PLEASE_MARKER)]
    return value


def _metadata_version(data, keys, location):
    value = data
    try:
        for key in keys:
            value = value[key]
    except (KeyError, TypeError) as error:
        raise VersionError(f"{location[0]} is missing {location[1]}") from error
    if not isinstance(value, str):
        raise VersionError(f"{location[0]} {location[1]} must be a string")
    return value


def _read_version_metadata(root):
    canonical = _read_canonical_version(root)
    pyproject = _load_toml(root, "pyproject.toml")
    package = _load_json(root, "package.json")
    package_lock = _load_json(root, "package-lock.json")
    metadata = {
        "pyproject.toml": _metadata_version(
            pyproject,
            ("project", "version"),
            ("pyproject.toml", "[project].version"),
        ),
        "package.json": _metadata_version(
            package,
            ("version",),
            ("package.json", "version"),
        ),
        "package-lock.json": _metadata_version(
            package_lock,
            ("version",),
            ("package-lock.json", "version"),
        ),
        'package-lock.json packages[""]': _metadata_version(
            package_lock,
            ("packages", "", "version"),
            ("package-lock.json", 'packages[""].version'),
        ),
    }
    manifest_path = root / ".release-please-manifest.json"
    if manifest_path.is_file():
        manifest = _load_json(root, ".release-please-manifest.json")
        metadata[".release-please-manifest.json"] = _metadata_version(
            manifest,
            (".",),
            (".release-please-manifest.json", 'root package version "."'),
        )
    return canonical, metadata


def _validate(root):
    canonical, metadata = _read_version_metadata(root)
    _parse_version(canonical)
    mismatches = [
        f"{name} version {value} does not match VERSION {canonical}"
        for name, value in metadata.items()
        if value != canonical
    ]
    if mismatches:
        raise VersionError("\n".join(mismatches))
    changelog = _read_text(root, "CHANGELOG.md")
    unreleased_headings = re.findall(
        r"(?m)^## (?:\[Unreleased\]|Unreleased)\s*$",
        changelog,
    )
    if not unreleased_headings:
        raise VersionError("CHANGELOG.md has no [Unreleased] section")
    if len(unreleased_headings) != 1:
        raise VersionError(
            "CHANGELOG.md must contain exactly one [Unreleased] section"
        )
    release_heading_pattern = (
        r"(?m)^## \[([^]]+)\](?:\([^\r\n]+\))? "
        r"(?:- \d{4}-\d{2}-\d{2}|\(\d{4}-\d{2}-\d{2}\))$"
    )
    release_versions = re.findall(release_heading_pattern, changelog)
    duplicates = sorted(
        {version for version in release_versions if release_versions.count(version) > 1}
    )
    if duplicates:
        raise VersionError(
            f"CHANGELOG.md has duplicate release entry: {', '.join(duplicates)}"
        )
    if not re.search(
        rf"^## \[{re.escape(canonical)}\](?:\([^\r\n]+\))? "
        rf"(?:- \d{{4}}-\d{{2}}-\d{{2}}|\(\d{{4}}-\d{{2}}-\d{{2}}\))$",
        changelog,
        re.MULTILINE,
    ):
        raise VersionError(f"CHANGELOG.md has no release entry for {canonical}")
    return canonical


def _check(root):
    canonical = _validate(root)
    print(f"Version metadata is consistent: {canonical}")
    return 0


def _updated_pyproject(source, version):
    project = re.search(r"(?ms)^\[project\]\s*$.*?(?=^\[|\Z)", source)
    if project is None:
        raise VersionError("pyproject.toml has no [project] table")
    updated, count = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]+"(?P<suffix>[ \t]*(?:# x-release-please-version)?[ \t]*)$',
        lambda match: f'{match.group(1)}"{version}"{match.group("suffix")}',
        project.group(0),
        count=1,
    )
    if count != 1:
        raise VersionError("pyproject.toml [project] table has no version")
    return source[: project.start()] + updated + source[project.end() :]


def _archived_changelog(source, version, release_date):
    unreleased = re.search(
        r"(?m)^## (?P<label>\[Unreleased\]|Unreleased)\s*$",
        source,
    )
    if unreleased is None:
        raise VersionError("CHANGELOG.md has no [Unreleased] section")
    if re.search(rf"(?m)^## \[{re.escape(version)}\](?:\s+-|\s*$)", source):
        raise VersionError(f"CHANGELOG.md already has release entry for {version}")
    body_start = unreleased.end()
    next_heading = re.search(r"(?m)^## ", source[body_start:])
    body_end = body_start + next_heading.start() if next_heading else len(source)
    body = source[body_start:body_end].strip()
    if not re.search(r"(?m)^-\s+\S", body):
        raise VersionError(
            "CHANGELOG.md [Unreleased] must contain at least one bullet "
            "before preparing a release"
        )
    previous_releases = source[body_end:].strip()
    prefix = source[: unreleased.start()].rstrip()
    pieces = [
        prefix,
        f"## {unreleased.group('label')}",
        f"## [{version}] - {release_date}",
        body,
    ]
    if previous_releases:
        pieces.append(previous_releases)
    return "\n\n".join(pieces) + "\n"


def _stage_file(target, content, suffix):
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.release-",
        suffix=suffix,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, stat.S_IMODE(target.stat().st_mode))
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _clean_up(paths):
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _replace_release_files(updates):
    staged = {}
    backups = {}
    try:
        for target, content in updates.items():
            backups[target] = _stage_file(target, target.read_bytes(), ".backup")
            staged[target] = _stage_file(target, content, ".new")
    except (OSError, KeyboardInterrupt) as error:
        _clean_up((*staged.values(), *backups.values()))
        if isinstance(error, KeyboardInterrupt):
            raise VersionError(
                "release preparation interrupted before file replacement; no files changed"
            ) from error
        raise VersionError(f"could not stage release files: {error}") from error

    replaced = []
    retained_backups = set()
    try:
        for target in updates:
            replaced.append(target)
            staged[target].replace(target)
    except (OSError, KeyboardInterrupt) as error:
        rollback_errors = []
        for target in reversed(replaced):
            try:
                backups[target].replace(target)
            except (OSError, KeyboardInterrupt) as rollback_error:
                retained_backups.add(backups[target])
                rollback_errors.append(f"{target.name}: {rollback_error}")
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            retained = ", ".join(str(path) for path in sorted(retained_backups))
            raise VersionError(
                "could not update release files and rollback was incomplete: "
                f"{detail}; recovery backup retained at: {retained}"
            ) from error
        if isinstance(error, KeyboardInterrupt):
            raise VersionError(
                "release preparation interrupted; original files restored"
            ) from error
        raise VersionError(
            f"could not update release files; original files restored: {error}"
        ) from error
    finally:
        _clean_up(staged.values())
        _clean_up(
            path for path in backups.values() if path not in retained_backups
        )


def _prepare(root, version, release_date):
    current = _validate(root)
    if _parse_version(version, "release version") <= _parse_version(current):
        raise VersionError(
            f"release version {version} must be greater than current version {current}"
        )
    _validate_release_date(release_date)

    pyproject_path = root / "pyproject.toml"
    package_path = root / "package.json"
    package_lock_path = root / "package-lock.json"
    changelog_path = root / "CHANGELOG.md"
    manifest_path = root / ".release-please-manifest.json"

    pyproject = _updated_pyproject(_read_text(root, "pyproject.toml"), version)
    package = _load_json(root, "package.json")
    package_lock = _load_json(root, "package-lock.json")
    changelog = _archived_changelog(
        _read_text(root, "CHANGELOG.md"),
        version,
        release_date,
    )
    package["version"] = version
    package_lock["version"] = version
    package_lock["packages"][""]["version"] = version

    version_suffix = (
        RELEASE_PLEASE_MARKER
        if _read_text(root, "VERSION").strip().endswith(RELEASE_PLEASE_MARKER)
        else ""
    )
    updates = {
        pyproject_path: pyproject.encode("utf-8"),
        package_path: (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        ),
        package_lock_path: (
            json.dumps(package_lock, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8"),
        changelog_path: changelog.encode("utf-8"),
        root / "VERSION": f"{version}{version_suffix}\n".encode("utf-8"),
    }
    if manifest_path.is_file():
        manifest = _load_json(root, ".release-please-manifest.json")
        manifest["."] = version
        updates[manifest_path] = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
    _replace_release_files(updates)
    print(f"Prepared release {version} ({release_date})")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage the product release version")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("show", help="print the current product version")
    subparsers.add_parser("check", help="verify version metadata and changelog consistency")
    prepare = subparsers.add_parser("prepare", help="prepare a new product release")
    prepare.add_argument("version", help="new MAJOR.MINOR.PATCH version")
    prepare.add_argument(
        "--date",
        dest="release_date",
        default=date.today().isoformat(),
        help="release date in YYYY-MM-DD format (default: today)",
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "show":
            print(_read_canonical_version(args.root))
            return 0
        if args.command == "check":
            return _check(args.root)
        if args.command == "prepare":
            return _prepare(args.root, args.version, args.release_date)
    except VersionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
