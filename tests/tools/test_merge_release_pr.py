from copy import deepcopy
from io import BytesIO
import json

import pytest

from tools.merge_release_pr import GitHub, ReleaseGateError, merge_when_ready


BASE = "a" * 40
HEAD = "b" * 40
TITLE = "chore: 发布 1.0.0"


class GitHubFixture:
    """External API boundary; merge requests are accepted only for the tested head."""

    repository = "example/project"

    def __init__(self):
        self.pull = {
            "number": 9, "state": "open", "draft": False, "title": TITLE, "body": "Release body\n",
            "head": {"sha": HEAD, "ref": "release-please--branches--main--components--family-phr",
                     "repo": {"full_name": self.repository}},
            "base": {"ref": "main", "sha": BASE},
        }
        self.base = BASE
        self.candidate_base = BASE
        self.runs = [{"id": 123, "head_sha": HEAD, "event": "pull_request",
                      "status": "completed", "conclusion": "success"}]
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success"}
                     for name in ("test", "postgres-concurrency", "container-build", "conventional-title")]
        self.requests = []
        self.pull_reads = 0
        self.before_pull_read = lambda: None

    def request(self, endpoint, *, payload=None):
        if endpoint == "pulls/9/merge":
            self.requests.append(payload)
            return {"merged": True, "sha": "c" * 40}
        if endpoint == "pulls/9":
            self.pull_reads += 1
            self.before_pull_read()
            return deepcopy(self.pull)
        if endpoint == "branches/main":
            return {"commit": {"sha": self.base}}
        if endpoint == f"compare/{BASE}...{HEAD}":
            return {"merge_base_commit": {"sha": self.candidate_base}, "status": "ahead", "behind_by": 0}
        if endpoint.startswith("actions/workflows/ci.yml/runs?"):
            return {"workflow_runs": deepcopy(self.runs)}
        if endpoint == "actions/runs/123/jobs?per_page=100":
            return {"total_count": len(self.jobs), "jobs": deepcopy(self.jobs)}
        raise AssertionError(f"Unexpected API request: {endpoint}")


def merge(api, **kwargs):
    return merge_when_ready(api, 9, BASE, **kwargs)


def test_waits_for_ci_registration_and_completion_before_squash():
    api = GitHubFixture()
    completed = api.runs
    api.runs = []
    sleeps = []

    def advance(_seconds):
        assert api.requests == []
        sleeps.append(True)
        api.runs = deepcopy(completed)
        if len(sleeps) == 1:
            api.runs[0].update(status="in_progress", conclusion=None)

    assert merge(api, sleep=advance) == "c" * 40
    assert len(sleeps) == 2
    assert api.requests == [{"sha": HEAD, "merge_method": "squash",
                             "commit_title": TITLE, "commit_message": "Release body\n"}]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped", None])
def test_unsuccessful_ci_never_merges(conclusion):
    api = GitHubFixture()
    api.runs[0]["conclusion"] = conclusion
    with pytest.raises(ReleaseGateError, match="CI did not succeed"):
        merge(api)
    assert api.requests == []


@pytest.mark.parametrize("defect", ["missing", "skipped", "running"])
def test_a_green_workflow_cannot_hide_a_required_job_that_did_not_pass(defect):
    api = GitHubFixture()
    if defect == "missing":
        api.jobs.pop(1)
    elif defect == "skipped":
        api.jobs[1]["conclusion"] = "skipped"
    else:
        api.jobs[1].update(status="in_progress", conclusion=None)
    with pytest.raises(ReleaseGateError, match="required CI jobs"):
        merge(api)
    assert api.requests == []


@pytest.mark.parametrize("change", ["head", "main", "body", "draft", "fork"])
def test_candidate_changes_during_final_check_prevent_merge(change):
    api = GitHubFixture()

    def change_candidate():
        if api.pull_reads < 2:
            return
        if change == "head":
            api.pull["head"]["sha"] = "d" * 40
        elif change == "main":
            api.base = "d" * 40
        elif change == "body":
            api.pull["body"] = "Changed release"
        elif change == "draft":
            api.pull["draft"] = True
        else:
            api.pull["head"]["repo"]["full_name"] = "other/fork"

    api.before_pull_read = change_candidate
    with pytest.raises(ReleaseGateError):
        merge(api)
    assert api.requests == []


def test_latest_failed_run_overrides_an_older_success():
    api = GitHubFixture()
    api.runs.append({**api.runs[0], "id": 124, "conclusion": "failure"})
    with pytest.raises(ReleaseGateError, match="CI did not succeed"):
        merge(api)
    assert api.requests == []


def test_candidate_must_contain_current_main_even_if_its_old_ci_passed():
    api = GitHubFixture()
    api.candidate_base = "d" * 40
    with pytest.raises(ReleaseGateError, match="does not include current main"):
        merge(api)
    assert api.requests == []


def test_no_matching_ci_times_out_without_merging():
    api = GitHubFixture()
    api.runs[0]["head_sha"] = "d" * 40
    with pytest.raises(ReleaseGateError, match="Timed out"):
        merge(api, timeout=0)
    assert api.requests == []


def test_declined_merge_is_reported_as_failure():
    api = GitHubFixture()
    request = api.request

    def decline(endpoint, *, payload=None):
        if payload is not None:
            return {"merged": False}
        return request(endpoint)

    api.request = decline
    with pytest.raises(ReleaseGateError, match="did not merge"):
        merge(api)


@pytest.mark.parametrize("write", [False, True])
def test_http_requests_separate_read_and_merge_credentials(monkeypatch, write):
    requests = []

    def send(request, *, timeout):
        requests.append(request)
        return BytesIO(b'{"ok": true}')

    monkeypatch.setattr("tools.merge_release_pr.urlopen", send)
    api = GitHub("example/project", "synthetic-read-token", "synthetic-merge-token")
    payload = {"commit_title": TITLE, "sha": HEAD, "merge_method": "squash"} if write else None
    endpoint = "pulls/9/merge" if write else "branches/main"

    assert api.request(endpoint, payload=payload) == {"ok": True}
    request = requests[0]
    assert request.full_url == "https://api.github.com/repos/example/project/" + endpoint
    assert request.method == ("PUT" if write else "GET")
    assert request.get_header("Authorization") == (
        "Bearer synthetic-merge-token" if write else "Bearer synthetic-read-token"
    )
    if write:
        assert json.loads(request.data) == {"commit_title": "chore: 发布 1.0.0",
                                           "sha": "b" * 40, "merge_method": "squash"}
    else:
        assert request.data is None
