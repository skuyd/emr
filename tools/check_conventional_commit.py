from __future__ import annotations

import argparse
import re
import sys


ALLOWED_TYPES = ("feat", "fix", "perf", "docs", "test", "chore", "ci", "refactor")
TITLE_PATTERN = re.compile(
    rf"^(?P<type>{'|'.join(ALLOWED_TYPES)})"
    r"(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?"
    r"(?P<breaking>!)?: (?P<description>\S(?:.*\S)?)$"
)
HAN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
BREAKING_FOOTER_PATTERN = re.compile(
    r"(?m)^BREAKING(?: |-)CHANGE:\s+\S"
)
BUMP_BY_TYPE = {
    "feat": "minor",
    "fix": "patch",
    "perf": "patch",
}
ERROR_MESSAGE = (
    "PR title must match '<type>(<scope>)!: 中文描述'; allowed types: "
    + ", ".join(ALLOWED_TYPES)
)


def _release_impact(match, body):
    if match.group("breaking") or BREAKING_FOOTER_PATTERN.search(body):
        return "major"
    return BUMP_BY_TYPE.get(match.group("type"), "none")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a Conventional Commit title")
    parser.add_argument("title")
    parser.add_argument("--body", default="")
    args = parser.parse_args(argv)

    match = TITLE_PATTERN.fullmatch(args.title)
    if match is None or HAN_PATTERN.search(match.group("description")) is None:
        print(f"ERROR: {ERROR_MESSAGE}", file=sys.stderr)
        return 1
    commit_type = match.group("type")
    print(
        f"Conventional commit is valid: {commit_type} "
        f"({_release_impact(match, args.body)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
