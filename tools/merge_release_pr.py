"""Wait for the release candidate's CI before requesting a Squash merge.

Uses the workflow token for reads and the Release Please token only for merging.
No branch-protection or GitHub auto-merge setting is assumed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from tools.check_conventional_commit import main as check_commit
except ModuleNotFoundError:  # Direct execution from tools/.
    from check_conventional_commit import main as check_commit


REQUIRED_JOBS = {"conventional-title", "test", "postgres-concurrency", "container-build"}
RELEASE_BRANCH = "release-please--branches--main--components--family-phr"


class ReleaseGateError(ValueError):
    pass


class GitHub:
    def __init__(self, repository, read_token, merge_token):
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
            raise ReleaseGateError("Invalid GitHub repository")
        self.repository = repository
        self.read_token = read_token
        self.merge_token = merge_token

    def request(self, endpoint, *, payload=None):
        request = Request(
            f"https://api.github.com/repos/{self.repository}/{endpoint}",
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method="GET" if payload is None else "PUT",
            headers={
                "Authorization": "Bearer " + (self.read_token if payload is None else self.merge_token),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "family-phr-release-gate",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise ReleaseGateError(f"GitHub API returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise ReleaseGateError("GitHub API request failed; no merge confirmed") from error


def _candidate(api, number, expected_base, original=None):
    pull = api.request(f"pulls/{number}")
    if (
        pull["state"] != "open" or pull["draft"]
        or pull["base"]["ref"] != "main"
        or pull["head"]["ref"] != RELEASE_BRANCH
        or (pull["head"].get("repo") or {}).get("full_name") != api.repository
        or re.fullmatch(r"chore: 发布 [0-9]+\.[0-9]+\.[0-9]+", pull["title"]) is None
    ):
        raise ReleaseGateError("PR is not an open release candidate from this repository")
    if (
        pull["base"]["sha"] != expected_base
        or api.request("branches/main")["commit"]["sha"] != expected_base
    ):
        raise ReleaseGateError("Main changed; Release Please must refresh the candidate")
    if original and (
        pull["head"]["sha"] != original["head"]["sha"]
        or pull["title"] != original["title"] or pull["body"] != original["body"]
    ):
        raise ReleaseGateError("Release candidate changed while checking CI")
    return pull


def merge_when_ready(api, number, expected_base, *, timeout=5400, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    pull = _candidate(api, number, expected_base)
    head = pull["head"]["sha"]
    ancestry = api.request(f"compare/{expected_base}...{head}")
    if (
        ancestry["merge_base_commit"]["sha"] != expected_base
        or ancestry["status"] != "ahead" or ancestry["behind_by"] != 0
    ):
        raise ReleaseGateError("Release candidate does not include current main; refresh it first")
    if check_commit([pull["title"], "--body", pull["body"] or ""]):
        raise ReleaseGateError("Release PR title failed Conventional Commit validation")
    while True:
        runs = api.request(
            f"actions/workflows/ci.yml/runs?event=pull_request&head_sha={head}&per_page=100"
        )["workflow_runs"]
        matching = [run for run in runs if run["head_sha"] == head and run["event"] == "pull_request"]
        run = max(matching, key=lambda item: item["id"]) if matching else None
        if run and run["status"] == "completed":
            if run["conclusion"] != "success":
                raise ReleaseGateError(f"Release CI did not succeed: {run['conclusion']}")
            result = api.request(f"actions/runs/{run['id']}/jobs?per_page=100")
            jobs = result["jobs"]
            passed = {job["name"] for job in jobs
                      if job["status"] == "completed" and job["conclusion"] == "success"}
            if result["total_count"] != len(jobs) or not REQUIRED_JOBS.issubset(passed):
                raise ReleaseGateError("Release is missing successful required CI jobs")
            _candidate(api, number, expected_base, pull)
            merged = api.request(f"pulls/{number}/merge", payload={
                "sha": head, "merge_method": "squash",
                "commit_title": pull["title"], "commit_message": pull["body"] or "",
            })
            if not merged.get("merged") or not merged.get("sha"):
                raise ReleaseGateError("GitHub did not merge the release PR")
            return merged["sha"]
        if clock() >= deadline:
            raise ReleaseGateError("Timed out waiting for the release candidate's CI")
        print(f"Waiting for CI on release PR #{number}, head {head}", flush=True)
        sleep(min(15, max(0, deadline - clock())))
        _candidate(api, number, expected_base, pull)


def main():
    try:
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"],
                     os.environ["RELEASE_PLEASE_TOKEN"])
        number = json.loads(os.environ["RELEASE_PR"])["number"]
        base = os.environ["GITHUB_SHA"]
        if type(number) is not int or number <= 0 or re.fullmatch(r"[0-9a-f]{40}", base) is None:
            raise ReleaseGateError("Invalid release PR number or expected base SHA")
        sha = merge_when_ready(api, number, base)
    except (ReleaseGateError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"ERROR: Release merge blocked: {error}", file=sys.stderr)
        return 1
    print(f"Release PR #{number} Squash merged after CI: {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
