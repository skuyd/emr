import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.submit import SubmitError, SessionLock, submit


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def git_input(repo, args, value):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=value.encode("utf-8")).decode().strip()


@pytest.fixture
def repository(tmp_path):
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "Submit Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "checkout", "-b", "main")
    (repo / "apps/example").mkdir(parents=True)
    (repo / "apps/example/module.py").write_text("base\n")
    (repo / "tests/example").mkdir(parents=True)
    (repo / "tests/example/test_example.py").write_text("def test_example():\n    assert True\n")
    (repo / "tests/tools").mkdir(parents=True)
    (repo / "tests/tools/test_release_version.py").write_text("def test_version():\n    assert True\n")
    (repo / "VERSION").write_text("1.0.0 # x-release-please-version\n")
    (repo / ".release-please-manifest.json").write_text('{".": "1.0.0"}\n')
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    (repo / "package.json").write_text('{"version": "1.0.0"}\n')
    (repo / "package-lock.json").write_text('{"version": "1.0.0", "packages": {"": {"version": "1.0.0"}}}\n')
    (repo / "CHANGELOG.md").write_text('# Changelog\n\n## [1.0.0] - 2026-09-01\nInitial\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "chore: 初始化")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "-b", "feat/example")
    (repo / "apps/example/module.py").write_text("feature\n")
    git(repo, "commit", "-am", "feat: 新增示例")
    return repo, remote


class FakeGitHub:
    repository = "skuyd/emr"

    def __init__(self, repo, remote):
        self.repo, self.remote = repo, remote
        self.pulls = {}
        self.merges = []
        self.enabled = False
        self.fail_after_merge = False
        self.tags = {}
        self.releases = {}
        self.label_updates = []

    def tag_sha(self, tag):
        return self.tags.get(tag)

    def release(self, tag):
        return self.releases.get(tag)

    def mark_released(self, number):
        self.pulls[number]["labels"] = [{"name": "autorelease: tagged"}]
        self.label_updates.append(number)

    def main_sha(self):
        return git(self.remote, "rev-parse", "refs/heads/main")

    def actions_disabled(self):
        return not self.enabled

    def find_pulls(self, branch):
        return [self.pull(n) for n, p in self.pulls.items() if p["head"]["ref"] == branch]

    def pull(self, number):
        return json.loads(json.dumps(self.pulls[number]))

    def upsert_pull(self, branch, title, body, head):
        matches = self.find_pulls(branch)
        p = matches[0] if matches else {"number": len(self.pulls) + 1}
        p.update(state="open", merged=False, draft=False, title=title, body=body,
                 head={"sha": head, "ref": branch, "repo": {"full_name": self.repository}},
                 base={"sha": self.main_sha(), "ref": "main"})
        p["labels"] = [{"name": "autorelease: pending"}] if branch.startswith("release-please--") else []
        self.pulls[p["number"]] = p
        return self.pull(p["number"])

    def merge(self, number, head, title, body):
        p = self.pulls[number]
        assert p["head"]["sha"] == head
        tree = git(self.repo, "rev-parse", head + "^{tree}")
        sha = git(self.repo, "commit-tree", tree, "-p", self.main_sha(), "-m", title)
        git(self.repo, "push", "origin", sha + ":refs/heads/main")
        p.update(merged=True, state="closed", merge_commit_sha=sha)
        self.merges.append((number, head, title, body))
        if self.fail_after_merge:
            self.fail_after_merge = False
            raise SubmitError("network response lost")
        return sha


def validate(repo, revision, state_dir, **kwargs):
    return {"status": "passed", "revision": revision,
            "tree": git(repo, "rev-parse", revision + "^{tree}"), "receipt_path": "receipt.json"}


def run(repo, api, **kwargs):
    return submit(repo, "feat/example", "feat: 新增示例", "测试正文", api=api,
                  validate=kwargs.pop("validate", validate), release_cli=kwargs.pop("release_cli", lambda _: None),
                  expected_origin=None, **kwargs)


@pytest.fixture
def completed_submission(repository, tmp_path):
    from tools.submit import common_dir, save_state
    import hashlib

    repo, remote = repository
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    merged = git(repo, "commit-tree", tree, "-p", "origin/main", "-m", "feat: 新增示例")
    git(repo, "push", "origin", merged + ":refs/heads/main", "feat/example")
    git(repo, "checkout", "main")
    worktree = tmp_path / "feature worktree"
    git(repo, "worktree", "add", str(worktree), "feat/example")
    directory = common_dir(repo) / "local-submit"
    directory.mkdir()
    path = directory / (hashlib.sha256(b"feat/example").hexdigest() + ".json")
    state = {"branch": "feat/example", "head": head, "status": "complete", "release": None,
             "origin": git(repo, "remote", "get-url", "origin"),
             "feature": {"branch": "feat/example", "head": head, "merged": merged, "pr": 1,
                         "receipt": {"tree": tree, "receipt_path": str(directory / "receipt.json")}}}
    save_state(path, state)
    (directory / "receipt.json").write_text("validation evidence\n")
    return repo, remote, worktree, path, state


def test_cleanup_removes_only_completed_branch_and_linked_worktree(completed_submission, monkeypatch):
    from tools import submit as module
    repo, remote, worktree, path, state = completed_submission
    (repo / "user-notes.txt").write_text("keep main workspace changes\n")
    other = repo.parent / "other-task"
    git(repo, "worktree", "add", "-b", "feat/other", str(other), "main")
    # Local excludes model ordinary ignored dependencies without changing HEAD.
    (module.common_dir(repo) / "info/exclude").write_text("node_modules/\n__pycache__/\n")
    (worktree / "node_modules").mkdir()
    (worktree / "node_modules/cache").write_text("regenerable\n")
    monkeypatch.chdir(worktree)

    result = module.cleanup_completed_submit(worktree, "feat/example")

    assert result["cleanup"]["status"] == "complete"
    assert not worktree.exists()
    assert not git(repo, "for-each-ref", "refs/heads/feat/example")
    assert not git(remote, "for-each-ref", "refs/heads/feat/example")
    assert (repo / "user-notes.txt").read_text() == "keep main workspace changes\n"
    assert other.is_dir() and git(other, "branch", "--show-current") == "feat/other"
    assert Path.cwd() == repo.resolve()
    assert (path.parent / "receipt.json").read_text() == "validation evidence\n"
    assert json.loads(path.read_text())["cleanup"]["status"] == "complete"
    # Retrying completed cleanup must never delete a newly reused branch name.
    git(repo, "branch", "feat/example", "main")
    assert module.cleanup_completed_submit(repo, "feat/example")["cleanup"]["status"] == "complete"
    assert git(repo, "for-each-ref", "refs/heads/feat/example")


@pytest.mark.parametrize("change", ["in_progress", "unmerged", "unpublished"])
def test_cleanup_requires_all_submit_stages_completed(completed_submission, change):
    from tools import submit as module
    repo, _, worktree, path, state = completed_submission
    if change == "in_progress":
        state["status"] = "in_progress"
    elif change == "unmerged":
        state["feature"].pop("merged")
    else:
        state["release"] = {"tag": "v1.1.0", "pr": 2}
    module.save_state(path, state)
    with pytest.raises(SubmitError, match="completed|published"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert worktree.exists()
    assert git(repo, "rev-parse", "refs/heads/feat/example") == state["head"]


@pytest.mark.parametrize("filename", ["apps/example/module.py", "notes.txt", ".env",
                                      "docs/deployment/local/tencent-cloud/access.txt"])
def test_cleanup_preserves_local_changes_and_private_ignored_files(completed_submission, filename):
    from tools import submit as module
    repo, _, worktree, _, state = completed_submission
    (module.common_dir(repo) / "info/exclude").write_text(".env\ndocs/deployment/local/tencent-cloud/\n")
    target = worktree / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("local content to preserve\n")
    with pytest.raises(SubmitError, match="local files|uncommitted"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert target.read_text() == "local content to preserve\n"
    assert git(repo, "rev-parse", "refs/heads/feat/example") == state["head"]


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_cleanup_preserves_changes_hidden_by_index_flags(completed_submission, flag):
    from tools import submit as module
    repo, _, worktree, _, _ = completed_submission
    filename = "apps/example/module.py"
    git(worktree, "update-index", flag, filename)
    (worktree / filename).write_text("hidden local changes\n")
    assert not git(worktree, "status", "--porcelain")
    with pytest.raises(SubmitError, match="index flags"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert (worktree / filename).read_text() == "hidden local changes\n"


def test_cleanup_preserves_branch_with_new_commits(completed_submission):
    from tools import submit as module
    repo, _, worktree, _, state = completed_submission
    git(worktree, "commit", "--allow-empty", "-m", "feat: 后续工作")
    with pytest.raises(SubmitError, match="head changed"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert worktree.is_dir()
    assert git(worktree, "rev-parse", "HEAD") != state["head"]


def test_cleanup_preserves_remote_branch_with_new_commits(completed_submission):
    from tools import submit as module
    repo, remote, worktree, _, state = completed_submission
    newer = git(repo, "commit-tree", state["feature"]["receipt"]["tree"],
                "-p", state["head"], "-m", "feat: 远端后续工作")
    git(repo, "push", "origin", newer + ":refs/heads/feat/example")
    with pytest.raises(SubmitError, match="remote branch head changed"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert worktree.is_dir()
    assert git(remote, "rev-parse", "refs/heads/feat/example") == newer


@pytest.mark.parametrize("multiple", [False, True])
def test_cleanup_rejects_different_push_destinations(completed_submission, multiple):
    from tools import submit as module
    repo, remote, worktree, _, state = completed_submission
    other_remote = repo.parent / "other-origin.git"
    subprocess.run(["git", "clone", "--bare", str(remote), str(other_remote)], check=True, capture_output=True)
    if multiple:
        git(repo, "config", "--add", "remote.origin.pushurl", str(remote))
    git(repo, "config", "--add", "remote.origin.pushurl", str(other_remote))
    with pytest.raises(SubmitError, match="push URL"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert git(remote, "rev-parse", "refs/heads/feat/example") == state["head"]
    assert git(other_remote, "rev-parse", "refs/heads/feat/example") == state["head"]
    assert worktree.is_dir()


def test_submit_rejects_different_push_destination_before_any_action(repository):
    repo, remote = repository
    git(repo, "config", "remote.origin.pushurl", str(repo.parent / "other-origin.git"))
    with pytest.raises(SubmitError, match="push URL"):
        run(repo, FakeGitHub(repo, remote))
    assert not git(remote, "for-each-ref", "refs/heads/feat/example")


def test_cleanup_uses_the_validated_worktrees_remote_configuration(completed_submission):
    from tools import submit as module
    repo, remote, worktree, _, state = completed_submission
    other_remote = repo.parent / "primary-push-origin.git"
    subprocess.run(["git", "clone", "--bare", str(remote), str(other_remote)], check=True, capture_output=True)
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "config", "--worktree", "remote.origin.pushurl", str(other_remote))
    assert git(worktree, "remote", "get-url", "--push", "origin") == str(remote)

    result = module.cleanup_completed_submit(worktree, "feat/example")

    assert result["cleanup"]["status"] == "complete"
    assert not git(remote, "for-each-ref", "refs/heads/feat/example")
    assert git(other_remote, "rev-parse", "refs/heads/feat/example") == state["head"]
    assert not worktree.exists()


def test_cleanup_preserves_worktree_containing_running_python(completed_submission, monkeypatch):
    from tools import submit as module
    repo, remote, worktree, _, state = completed_submission
    executable = worktree / ".venv/Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"running interpreter placeholder")
    (module.common_dir(repo) / "info/exclude").write_text(".venv/\n")
    monkeypatch.setattr(module.sys, "executable", str(executable))
    with pytest.raises(SubmitError, match="Python interpreter"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert (worktree / ".git").is_file()
    assert git(remote, "rev-parse", "refs/heads/feat/example") == state["head"]


def test_cleanup_is_bound_to_the_just_completed_submission(completed_submission):
    from tools import submit as module
    repo, remote, worktree, _, state = completed_submission
    with pytest.raises(SubmitError, match="record changed"):
        module.cleanup_completed_submit(repo, "feat/example", expected_head="0" * 40)
    assert worktree.is_dir()
    assert git(remote, "rev-parse", "refs/heads/feat/example") == state["head"]


def test_cleanup_remote_delete_lease_preserves_racing_commit(completed_submission, monkeypatch):
    from tools import submit as module
    repo, remote, worktree, _, state = completed_submission
    newer = git(repo, "commit-tree", state["feature"]["receipt"]["tree"],
                "-p", state["head"], "-m", "feat: 远端并发工作")
    original_git = module.git
    def advance(repo, *args):
        if args[0] == "push":
            git(repo, "push", "origin", newer + ":refs/heads/feat/example")
        return original_git(repo, *args)
    monkeypatch.setattr(module, "git", advance)
    with pytest.raises(SubmitError, match="Git push failed"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert worktree.is_dir()
    assert git(remote, "rev-parse", "refs/heads/feat/example") == newer


@pytest.mark.parametrize("change", ["commit", "checkout", "private-file"])
def test_cleanup_rechecks_local_work_after_remote_delete(completed_submission, monkeypatch, change):
    from tools import submit as module
    repo, _, worktree, _, state = completed_submission
    (module.common_dir(repo) / "info/exclude").write_text(".env\n")
    original_git = module.git
    def change_local(repo, *args):
        result = original_git(repo, *args)
        if args[0] == "push":
            if change == "commit":
                git(worktree, "commit", "--allow-empty", "-m", "feat: 本地并发工作")
            elif change == "checkout":
                git(worktree, "checkout", "-b", "feat/new-task")
            else:
                (worktree / ".env").write_text("private local content\n")
        return result
    monkeypatch.setattr(module, "git", change_local)
    with pytest.raises(SubmitError, match="changed|local files"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert worktree.is_dir()
    assert git(repo, "for-each-ref", "refs/heads/feat/example")


def test_cleanup_keeps_nested_worktree(completed_submission):
    from tools import submit as module
    repo, _, worktree, _, _ = completed_submission
    nested = worktree / "other-task"
    git(repo, "worktree", "add", "-b", "feat/nested", str(nested), "main")
    with pytest.raises(SubmitError, match="another worktree"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert nested.is_dir() and worktree.is_dir()


def test_cleanup_recovers_after_refs_deleted_before_state_saved(completed_submission, monkeypatch):
    from tools import submit as module
    repo, remote, worktree, path, _ = completed_submission
    original_save = module.save_state
    def interrupt(path, state):
        if state.get("cleanup", {}).get("status") == "complete":
            raise OSError("interrupted state save")
        original_save(path, state)
    monkeypatch.setattr(module, "save_state", interrupt)
    with pytest.raises(OSError, match="interrupted state save"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert not worktree.exists()
    assert not git(repo, "for-each-ref", "refs/heads/feat/example")
    assert not git(remote, "for-each-ref", "refs/heads/feat/example")
    assert json.loads(path.read_text())["cleanup"]["status"] == "pending"
    monkeypatch.setattr(module, "save_state", original_save)
    assert module.cleanup_completed_submit(repo, "feat/example")["cleanup"]["status"] == "complete"


def test_cleanup_keeps_primary_workspace(completed_submission):
    from tools import submit as module
    repo, _, worktree, _, _ = completed_submission
    git(repo, "worktree", "remove", str(worktree))
    git(repo, "checkout", "feat/example")
    with pytest.raises(SubmitError, match="primary worktree"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert repo.is_dir()
    assert git(repo, "branch", "--show-current") == "feat/example"


def test_cleanup_recovers_after_worktree_removed_without_repeating_submit(completed_submission, monkeypatch):
    from tools import submit as module
    repo, _, worktree, path, state = completed_submission
    original_git = module.git
    def interrupt(repo, *args):
        if args[:2] == ("update-ref", "-d"):
            raise SubmitError("interrupted branch cleanup")
        return original_git(repo, *args)
    monkeypatch.setattr(module, "git", interrupt)
    with pytest.raises(SubmitError, match="interrupted branch cleanup"):
        module.cleanup_completed_submit(repo, "feat/example")
    assert not worktree.exists()
    assert git(repo, "rev-parse", "refs/heads/feat/example") == state["head"]
    assert json.loads(path.read_text())["cleanup"]["status"] == "pending"
    monkeypatch.setattr(module, "git", original_git)
    monkeypatch.setattr(module, "ROOT", repo)
    def no_submit(*args, **kwargs):
        raise AssertionError("cleanup recovery must not repeat submission or tests")
    monkeypatch.setattr(module, "submit", no_submit)
    assert module.main(["--branch", "feat/example", "--cleanup-only"]) == 0
    assert not git(repo, "for-each-ref", "refs/heads/feat/example")


@pytest.mark.parametrize("outcome", ["complete", "failed", "dry-run"])
def test_cli_runs_cleanup_only_after_success(repository, tmp_path, monkeypatch, outcome):
    from tools import submit as module
    repo, _ = repository
    body = tmp_path / "body.txt"
    body.write_text("body")
    calls = []
    def submit_result(*args, **kwargs):
        calls.append("submit")
        if outcome == "failed":
            raise SubmitError("validation failed")
        return {"status": outcome, "branch": "feat/example", "head": "a" * 40}
    def cleanup(*args, **kwargs):
        calls.append("cleanup")
        assert kwargs == {"expected_head": "a" * 40}
        return {"cleanup": {"status": "complete"}}
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setattr(module, "submit", submit_result)
    monkeypatch.setattr(module, "cleanup_completed_submit", cleanup, raising=False)
    assert module.main(["--title", "feat: 新增示例", "--body-file", str(body)]) == (1 if outcome == "failed" else 0)
    assert calls == (["submit", "cleanup"] if outcome == "complete" else ["submit"])


def test_validation_failure_never_pushes_or_merges(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    def fail(*args, **kwargs):
        raise SubmitError("tests failed")
    with pytest.raises(SubmitError, match="tests failed"):
        run(repo, api, validate=fail)
    assert not api.merges
    assert not git(remote, "for-each-ref", "refs/heads/feat/example")


def test_no_release_and_squash_is_bound_to_validated_sha(repository):
    repo, remote = repository
    head = git(repo, "rev-parse", "HEAD")
    api = FakeGitHub(repo, remote)
    commands = []
    result = run(repo, api, release_cli=commands.append)
    assert result["status"] == "complete"
    assert result["release"] is None
    assert api.merges == [(1, head, "feat: 新增示例", "测试正文")]
    assert commands == ["release-pr"]
    assert git(repo, "branch", "--show-current") == "feat/example"


def test_merge_timeout_recovers_remote_fact_without_second_merge(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    api.fail_after_merge = True
    with pytest.raises(SubmitError, match="response lost"):
        run(repo, api)
    assert run(repo, api)["status"] == "complete"
    assert len(api.merges) == 1


def test_remote_main_advancing_after_validation_stops(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    def advance(*args, **kwargs):
        receipt = validate(*args, **kwargs)
        sha = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"),
                  "-p", api.main_sha(), "-m", "chore: 其他任务")
        git(repo, "push", "origin", sha + ":refs/heads/main")
        return receipt
    with pytest.raises(SubmitError, match="main changed"):
        run(repo, api, validate=advance)
    assert not api.merges


def test_actions_must_be_disabled_before_mutation(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    api.enabled = True
    with pytest.raises(SubmitError, match="Actions"):
        run(repo, api)
    assert not api.merges
    assert not api.pulls


def test_dirty_worktree_and_lock_prevent_submit(repository, tmp_path):
    repo, remote = repository
    (repo / "uncommitted.txt").write_text("belongs to another task")
    with pytest.raises(SubmitError, match="clean"):
        run(repo, FakeGitHub(repo, remote))
    with SessionLock(tmp_path / "lock"):
        with pytest.raises(SubmitError, match="another submit"):
            with SessionLock(tmp_path / "lock"):
                pass


def test_dry_run_does_not_create_state_or_contact_api(repository):
    repo, remote = repository
    before = set(Path(git(repo, "rev-parse", "--absolute-git-dir")).rglob("*"))
    result = run(repo, None, dry_run=True)
    after = set(Path(git(repo, "rev-parse", "--absolute-git-dir")).rglob("*"))
    assert result["status"] == "dry-run"
    assert before == after


def recommended_plan(repo):
    return {"mode": "planned", "groups": ["django", "python", "production-build", "production-smoke"],
            "reason": "Shared configuration affects multiple modules",
            "changed_paths": ["apps/example/module.py"],
            "base_revision": git(repo, "rev-parse", "origin/main"),
            "revision": git(repo, "rev-parse", "HEAD"),
            "targets": {"python": ["tests/app/test_example.py"], "browser": [],
                        "postgres": [], "javascript": []},
            "full_recommended": True,
            "risks": ["Other Django modules are outside the selected tests"]}


def test_full_recommendation_stops_before_tests_or_remote_writes(repository, monkeypatch, capsys):
    import tools.submit as module
    repo, remote = repository
    monkeypatch.setattr(module, "validation_plan_for", lambda *_: recommended_plan(repo))
    api = FakeGitHub(repo, remote)
    with pytest.raises(SubmitError, match="--full-tests.*--plan-digest"):
        run(repo, api, validate=lambda *_args, **_kwargs: pytest.fail("validation ran before a decision"))
    output = capsys.readouterr().out
    assert "tests/app/test_example.py" in output
    assert "python tools/run_required_tests.py" in output
    assert "Other Django modules" in output
    assert "Shared configuration" in output
    assert not api.pulls and not api.merges


def test_explicit_related_choice_runs_scoped_tests_and_records_risk(repository, monkeypatch):
    import tools.submit as module
    repo, remote = repository
    monkeypatch.setattr(module, "validation_plan_for", lambda *_: recommended_plan(repo))
    plan = recommended_plan(repo)
    digest = module.validation_plan_digest(plan)
    calls = []
    def record(*args, **kwargs):
        calls.append(kwargs)
        return validate(*args, **kwargs)
    result = run(repo, FakeGitHub(repo, remote), validate=record,
                 full_tests="skip", plan_digest=digest)
    assert result["status"] == "complete"
    assert calls == [{"mode": "planned", "base_revision": plan["base_revision"],
                      "scope_decision": "skip", "plan_digest": digest}]
    assert result["feature"]["validation_decision"]["choice"] == "skip"
    assert result["feature"]["validation_decision"]["plan_digest"] == digest


def test_explicit_full_choice_runs_full_tests_for_exact_plan(repository, monkeypatch):
    import tools.submit as module
    repo, remote = repository
    monkeypatch.setattr(module, "validation_plan_for", lambda *_: recommended_plan(repo))
    plan = recommended_plan(repo)
    digest = module.validation_plan_digest(plan)
    calls = []
    def record(*args, **kwargs):
        calls.append(kwargs)
        return validate(*args, **kwargs)
    result = run(repo, FakeGitHub(repo, remote), validate=record,
                 full_tests="run", plan_digest=digest)
    assert result["status"] == "complete"
    assert calls == [{"mode": "full", "base_revision": plan["base_revision"],
                      "scope_decision": "run", "plan_digest": digest}]


def test_stale_choice_is_rejected_when_current_plan_no_longer_recommends_full(repository, monkeypatch):
    import tools.submit as module
    repo, remote = repository
    plan = recommended_plan(repo)
    old_digest = module.validation_plan_digest(plan)
    plan.update(full_recommended=False, risks=[])
    monkeypatch.setattr(module, "validation_plan_for", lambda *_: plan)
    api = FakeGitHub(repo, remote)
    with pytest.raises(SubmitError, match="current plan"):
        run(repo, api, validate=lambda *_args, **_kwargs: pytest.fail("stale choice ran validation"),
            full_tests="skip", plan_digest=old_digest)
    assert not api.pulls and not api.merges


def test_full_choice_digest_from_old_candidate_cannot_authorize_new_candidate(repository, monkeypatch):
    import tools.submit as module
    repo, remote = repository
    monkeypatch.setattr(module, "validation_plan_for", lambda *_: recommended_plan(repo))
    digest = module.validation_plan_digest(recommended_plan(repo))
    (repo / "apps/example/module.py").write_text("another candidate\n")
    git(repo, "commit", "-am", "feat: 更新示例")
    with pytest.raises(SubmitError, match="digest"):
        run(repo, FakeGitHub(repo, remote), validate=lambda *_args, **_kwargs: pytest.fail("stale choice ran validation"),
            full_tests="skip", plan_digest=digest)


def test_feature_choice_survives_retry_for_same_candidate(repository, monkeypatch):
    import tools.submit as module
    repo, remote = repository
    monkeypatch.setattr(module, "validation_plan_for", lambda *_: recommended_plan(repo))
    digest = module.validation_plan_digest(recommended_plan(repo))
    api = FakeGitHub(repo, remote)
    upsert = api.upsert_pull
    attempts = []
    def interrupted(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise SubmitError("PR response unavailable")
        return upsert(*args)
    api.upsert_pull = interrupted
    with pytest.raises(SubmitError, match="response unavailable"):
        run(repo, api, full_tests="skip", plan_digest=digest)
    result = run(repo, api)
    assert result["status"] == "complete"
    assert result["feature"]["validation_decision"]["plan_digest"] == digest
    assert len(attempts) == 2


def release_runner(repo, api, commands, *, fail_after_publish=False):
    from tools.submit import RELEASE_BRANCH
    def execute(command):
        commands.append(command)
        if command == "release-pr":
            changes = {
                "VERSION": "1.1.0 # x-release-please-version\n",
                ".release-please-manifest.json": '{".": "1.1.0"}\n',
                "pyproject.toml": '[project]\nversion = "1.1.0"\n',
                "package.json": '{"version": "1.1.0"}\n',
                "package-lock.json": '{"version": "1.1.0", "packages": {"": {"version": "1.1.0"}}}\n',
                "CHANGELOG.md": '# Changelog\n\n## [1.1.0] - 2026-09-30\nFeature\n\n## [1.0.0] - 2026-09-01\nInitial\n',
            }
            blobs = {name: git_input(repo, ["hash-object", "-w", "--stdin"], text)
                     for name, text in changes.items()}
            entries = git(repo, "ls-tree", api.main_sha()).splitlines()
            entries = [f"100644 blob {blobs[line.split(chr(9), 1)[1]]}\t{line.split(chr(9), 1)[1]}"
                       if line.split("\t", 1)[1] in blobs else line for line in entries]
            tree = git_input(repo, ["mktree"], "\n".join(entries) + "\n")
            sha = git(repo, "commit-tree", tree,
                      "-p", api.main_sha(), "-m", "chore: 发布 1.1.0")
            git(repo, "push", "origin", sha + ":refs/heads/" + RELEASE_BRANCH)
            api.upsert_pull(RELEASE_BRANCH, "chore: 发布 1.1.0", "发行说明", sha)
        else:
            api.tags["v1.1.0"] = api.main_sha()
            api.releases["v1.1.0"] = {"tag_name": "v1.1.0", "draft": False,
                                        "html_url": "https://github.com/skuyd/emr/releases/tag/v1.1.0"}
            if fail_after_publish:
                raise SubmitError("publish response lost")
    execute.preview = lambda: [{"pr": 2, "tag": "v1.1.0", "sha": api.pulls[2]["merge_commit_sha"],
                               "draft": False, "prerelease": False, "force_tag": False}]
    return execute


def test_release_candidate_is_validated_and_published_with_exact_merge_tree(repository):
    repo, remote = repository
    api, commands, receipts = FakeGitHub(repo, remote), [], []
    def record(*args, **kwargs):
        receipts.append(kwargs)
        return validate(*args, **kwargs)
    result = run(repo, api, validate=record, release_cli=release_runner(repo, api, commands))
    assert commands == ["release-pr", "github-release"]
    assert len(api.merges) == 2
    assert receipts[-1] == {"mode": "release", "baseline_receipt": "receipt.json"}
    assert api.tags["v1.1.0"] == result["release"]["merged"]


def test_release_publish_timeout_does_not_republish_or_remerge(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    runner = release_runner(repo, api, commands, fail_after_publish=True)
    with pytest.raises(SubmitError, match="publish response lost"):
        run(repo, api, release_cli=runner)
    assert run(repo, api, release_cli=runner)["status"] == "complete"
    assert commands == ["release-pr", "github-release"]
    assert len(api.merges) == 2


def test_changed_remote_pr_head_prevents_merge(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    original = api.upsert_pull
    def changed(*args):
        pull = original(*args)
        api.pulls[pull["number"]]["head"]["sha"] = "a" * 40
        return pull
    api.upsert_pull = changed
    with pytest.raises(SubmitError, match="candidate changed"):
        run(repo, api)
    assert not api.merges


def test_invalid_validation_receipt_cannot_merge(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    def invalid(*args, **kwargs):
        receipt = validate(*args, **kwargs)
        receipt["tree"] = "b" * 40
        return receipt
    with pytest.raises(SubmitError, match="receipt"):
        run(repo, api, validate=invalid)
    assert not api.merges


def test_existing_wrong_tag_is_never_overwritten(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    api.tags["v1.1.0"] = "a" * 40
    with pytest.raises(SubmitError, match="never overwritten"):
        run(repo, api, release_cli=release_runner(repo, api, commands))
    assert commands == ["release-pr"]
    assert api.tags["v1.1.0"] == "a" * 40


def test_release_validation_failure_never_merges_release_pr(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    def reject_release(*args, **kwargs):
        if kwargs.get("mode") == "release":
            raise SubmitError("release validation failed")
        return validate(*args, **kwargs)
    with pytest.raises(SubmitError, match="release validation failed"):
        run(repo, api, validate=reject_release, release_cli=release_runner(repo, api, commands))
    assert len(api.merges) == 1
    assert commands == ["release-pr"]


def test_release_cli_token_is_only_in_environment(repository, monkeypatch):
    from tools.submit import ReleasePlease
    from types import SimpleNamespace
    repo, remote = repository
    cli = repo / "node_modules/release-please/build/src/bin/release-please.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("test fake CLI")
    calls = []
    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", capture)
    ReleasePlease(repo, "test-only-secret")("release-pr")
    argv, kwargs = calls[0]
    assert "test-only-secret" not in str(argv)
    assert kwargs["env"]["RELEASE_PLEASE_TOKEN"] == "test-only-secret"
    assert ".env('RELEASE_PLEASE')" in argv[2]


def test_private_deployment_content_in_history_prevents_push(repository):
    repo, remote = repository
    private = repo / "docs/deployment/local/tencent-cloud/secret.txt"
    private.parent.mkdir(parents=True)
    private.write_text("synthetic test only")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "chore: 临时文件")
    private.unlink()
    git(repo, "commit", "-am", "chore: 移除文件")
    api = FakeGitHub(repo, remote)
    with pytest.raises(SubmitError, match="Private deployment"):
        run(repo, api)
    assert not api.pulls


def test_failed_local_validation_can_resume_after_committed_fix(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    def fail(*args, **kwargs):
        raise SubmitError("tests failed")
    with pytest.raises(SubmitError, match="tests failed"):
        run(repo, api, validate=fail)
    (repo / "apps/example/module.py").write_text("fixed feature\n")
    git(repo, "commit", "-am", "fix: 修复验证失败")
    assert run(repo, api)["status"] == "complete"


def test_worktrees_resolve_the_same_session_lock(repository, tmp_path):
    from tools.submit import common_dir
    repo, remote = repository
    other = tmp_path / "other"
    git(repo, "worktree", "add", "--detach", str(other), "HEAD")
    assert common_dir(repo) == common_dir(other)
    with SessionLock(common_dir(repo) / "local-submit/session.lock"):
        with pytest.raises(SubmitError, match="another submit"):
            with SessionLock(common_dir(other) / "local-submit/session.lock"):
                pass


def test_main_advancing_after_feature_merge_validates_only_final_release_candidate(repository):
    repo, remote = repository
    api, commands, revisions = FakeGitHub(repo, remote), [], []
    def stopped(command):
        raise SubmitError("release unavailable")
    with pytest.raises(SubmitError, match="release unavailable"):
        run(repo, api, release_cli=stopped)
    new_main = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"),
                   "-p", api.main_sha(), "-m", "docs: 后续说明")
    git(repo, "push", "origin", new_main + ":refs/heads/main")
    def record(repo, revision, state_dir, **kwargs):
        revisions.append((revision, kwargs.get("mode", "full")))
        return validate(repo, revision, state_dir, **kwargs)
    result = run(repo, api, validate=record, release_cli=release_runner(repo, api, commands))
    assert result["status"] == "complete"
    assert revisions == [(result["release"]["head"], "release")]
    assert len(api.merges) == 2


def test_release_tool_change_stops_before_validation_until_local_checkout_matches(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    def pause(_command):
        raise SubmitError("pause before release candidate")
    with pytest.raises(SubmitError, match="pause before release"):
        run(repo, api, release_cli=pause)
    parent = api.main_sha()
    blob = git_input(repo, ["hash-object", "-w", "--stdin"], "# newer validation runner\n")
    tools_tree = git_input(repo, ["mktree"], f"100644 blob {blob}\tlocal_validation.py\n")
    entries = git(repo, "ls-tree", parent).splitlines()
    entries.append(f"040000 tree {tools_tree}\ttools")
    tree = git_input(repo, ["mktree"], "\n".join(entries) + "\n")
    newer_main = git(repo, "commit-tree", tree, "-p", parent, "-m", "chore: 更新验证工具")
    git(repo, "push", "origin", newer_main + ":refs/heads/main")
    runner = release_runner(repo, api, commands)
    calls = []
    def record(*args, **kwargs):
        calls.append(kwargs["mode"])
        return validate(*args, **kwargs)
    with pytest.raises(SubmitError, match="tools/local_validation.py.*synchronize"):
        run(repo, api, validate=record, release_cli=runner)
    assert calls == []
    assert len(api.merges) == 1
    git(repo, "checkout", "--detach", api.pull(2)["head"]["sha"])
    result = run(repo, api, validate=record, release_cli=runner)
    assert result["status"] == "complete"
    assert calls == ["release"]


def test_feature_tool_change_in_current_checkout_stops_before_plan_or_tests(repository):
    repo, remote = repository
    git(repo, "checkout", "-b", "recovery", "origin/main")
    (repo / "tools").mkdir()
    (repo / "tools/local_validation.py").write_text("# changed local runner\n")
    git(repo, "add", "tools/local_validation.py")
    git(repo, "commit", "-m", "chore: 更新本地验证工具")
    api = FakeGitHub(repo, remote)
    with pytest.raises(SubmitError, match="tools/local_validation.py.*synchronize"):
        run(repo, api, validate=lambda *_args, **_kwargs: pytest.fail("mismatched tools ran validation"))
    assert not api.pulls and not api.merges


def test_docs_only_candidate_selects_documentation_validation(repository):
    repo, remote = repository
    git(repo, "restore", "--source=origin/main", "apps/example/module.py")
    (repo / "docs").mkdir()
    (repo / "docs/README.md").write_text("Documentation update\n")
    git(repo, "add", "apps/example/module.py", "docs/README.md")
    git(repo, "commit", "-m", "docs: 更新说明")
    api = FakeGitHub(repo, remote)
    base = api.main_sha()
    calls = []
    def record(repo, revision, state_dir, **kwargs):
        calls.append((revision, kwargs))
        return validate(repo, revision, state_dir, **kwargs)
    result = run(repo, api, validate=record)
    assert result["status"] == "complete"
    assert calls == [(result["head"], {"mode": "docs", "base_revision": base})]
    assert len(api.merges) == 1


def workflow_candidate(repo):
    git(repo, "restore", "--source=origin/main", "apps/example/module.py")
    (repo / "tools").mkdir()
    (repo / "tools/submit.py").write_text("# workflow candidate\n")
    git(repo, "add", "apps/example/module.py", "tools/submit.py")
    git(repo, "commit", "-m", "fix: 修复提交流程")


def test_workflow_change_prints_affected_groups_and_never_requests_business_full(repository, capsys):
    repo, remote = repository
    workflow_candidate(repo)
    api, calls = FakeGitHub(repo, remote), []
    def record(repo, revision, state_dir, **kwargs):
        calls.append(kwargs)
        return validate(repo, revision, state_dir, **kwargs)
    result = run(repo, api, validate=record)
    assert calls == [{"mode": "planned", "base_revision": result["feature"]["base"]}]
    assert result["feature"]["validation_plan"]["groups"] == [
        "contracts", "workflow", "production-build", "production-smoke"]
    assert "workflow" in capsys.readouterr().out


def test_unknown_impact_stops_before_validation_or_remote_writes(repository):
    repo, remote = repository
    git(repo, "restore", "--source=origin/main", "apps/example/module.py")
    (repo / "unclassified-input.bin").write_bytes(b"unknown impact")
    git(repo, "add", "apps/example/module.py", "unclassified-input.bin")
    git(repo, "commit", "-m", "chore: 合成未知输入")
    api = FakeGitHub(repo, remote)
    def unexpected(*args, **kwargs):
        raise AssertionError("An unclassified change must not silently run full")
    with pytest.raises(SubmitError, match="unclassified-input.bin"):
        run(repo, api, validate=unexpected)
    assert not api.merges and not api.pulls


def test_workflow_release_reuse_rejection_does_not_expand_to_business_full(repository):
    from tools.submit_validation import ReuseUnavailable
    repo, remote = repository
    workflow_candidate(repo)
    api, commands, modes = FakeGitHub(repo, remote), [], []
    def reject(*args, **kwargs):
        mode = kwargs.get("mode", "full")
        modes.append(mode)
        if mode == "release":
            raise ReuseUnavailable("No matching evidence for release")
        return validate(*args, **kwargs)
    with pytest.raises(ReuseUnavailable, match="No matching evidence"):
        run(repo, api, validate=reject, release_cli=release_runner(repo, api, commands))
    assert modes == ["planned", "release", "planned", "release"]
    assert len(api.merges) == 1


def test_workflow_release_uses_specialist_evidence_without_business_full(repository):
    repo, remote = repository
    workflow_candidate(repo)
    api, commands, modes = FakeGitHub(repo, remote), [], []
    def record(*args, **kwargs):
        modes.append(kwargs.get("mode", "full"))
        return validate(*args, **kwargs)
    result = run(repo, api, validate=record, release_cli=release_runner(repo, api, commands))
    assert result["status"] == "complete"
    assert modes == ["planned", "release"]


def test_resumed_submit_without_release_does_not_validate_advanced_main(repository):
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    def stopped(command):
        raise SubmitError("release unavailable")
    with pytest.raises(SubmitError, match="release unavailable"):
        run(repo, api, release_cli=stopped)
    new_main = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"),
                   "-p", api.main_sha(), "-m", "docs: 后续说明")
    git(repo, "push", "origin", new_main + ":refs/heads/main")
    def unexpected(*args, **kwargs):
        raise AssertionError("No release candidate needs validation")
    result = run(repo, api, validate=unexpected)
    assert result["status"] == "complete"
    assert result["release"] is None
    assert len(api.merges) == 1


def test_release_reuse_rejection_revalidates_main_scope_then_release(repository):
    from tools.submit_validation import ReuseUnavailable
    repo, remote = repository
    api, commands, calls = FakeGitHub(repo, remote), [], []
    def changed_environment(*args, **kwargs):
        mode = kwargs.get("mode", "full")
        calls.append((args[1], kwargs))
        if mode == "release" and len([call for call in calls if call[1].get("mode") == "release"]) == 1:
            raise ReuseUnavailable("Release baseline environment does not match")
        return validate(*args, **kwargs)
    result = run(repo, api, validate=changed_environment, release_cli=release_runner(repo, api, commands))
    assert result["status"] == "complete"
    assert [kwargs["mode"] for _, kwargs in calls] == ["planned", "release", "planned", "release"]
    assert calls[2][0] == result["release"]["head"]
    assert calls[2][1]["base_revision"] == result["feature"]["base"]
    assert calls[2][1]["plan_revision"] == result["release"]["base"]
    assert calls[3][1]["baseline_receipt"] == "receipt.json"


def test_release_fallback_requires_choice_for_new_release_candidate(repository):
    import tools.submit as module
    from tools.submit_validation import ReuseUnavailable, select_validation_plan

    repo, remote = repository
    (repo / "config").mkdir()
    (repo / "config/settings.py").write_text("CONFIG = True\n")
    (repo / "tests/test_project_configuration.py").write_text("def test_configuration():\n    assert True\n")
    git(repo, "add", "config/settings.py", "tests/test_project_configuration.py")
    git(repo, "commit", "-m", "feat: 调整配置")
    base = git(repo, "rev-parse", "origin/main")
    feature_plan = select_validation_plan(repo, base, git(repo, "rev-parse", "HEAD"))
    assert feature_plan["full_recommended"]
    feature_digest = module.validation_plan_digest(feature_plan)
    api, commands, modes = FakeGitHub(repo, remote), [], []
    def unavailable_once(*args, **kwargs):
        mode = kwargs.get("mode", "full")
        modes.append(mode)
        if mode == "release" and modes.count("planned") + modes.count("full") < 2:
            raise ReuseUnavailable("Baseline environment changed")
        return validate(*args, **kwargs)
    runner = release_runner(repo, api, commands)
    with pytest.raises(SubmitError, match="digest"):
        run(repo, api, validate=unavailable_once, release_cli=runner,
            full_tests="skip", plan_digest=feature_digest)
    assert modes == ["planned", "release"]
    assert len(api.merges) == 1
    release = api.pull(2)
    release_plan = select_validation_plan(repo, base, release["base"]["sha"])
    release_digest = module.validation_plan_digest(release_plan, release["head"]["sha"])
    assert release_digest != feature_digest
    result = run(repo, api, validate=unavailable_once, release_cli=runner,
                 full_tests="run", plan_digest=release_digest)
    assert result["status"] == "complete"
    assert modes == ["planned", "release", "release", "full", "release"]
    assert result["release"]["validation_decision"]["plan_digest"] == release_digest
    assert result["release"]["validation_decision"]["choice"] == "run"
    assert result["release"]["validation_decision"]["head"] == release["head"]["sha"]


def test_docs_only_main_difference_cannot_become_release_business_baseline(repository):
    from tools.submit_validation import ReuseUnavailable

    repo, remote = repository
    git(repo, "restore", "--source=origin/main", "apps/example/module.py")
    (repo / "docs").mkdir()
    (repo / "docs/README.md").write_text("Documentation update\n")
    git(repo, "add", "apps/example/module.py", "docs/README.md")
    git(repo, "commit", "-m", "docs: 更新说明")
    api, commands, modes = FakeGitHub(repo, remote), [], []
    def no_baseline(*args, **kwargs):
        modes.append(kwargs["mode"])
        if kwargs["mode"] == "release":
            raise ReuseUnavailable("No reusable business baseline")
        return validate(*args, **kwargs)
    with pytest.raises(SubmitError, match="no reusable business validation baseline"):
        run(repo, api, validate=no_baseline, release_cli=release_runner(repo, api, commands))
    assert modes == ["docs", "release"]
    assert len(api.merges) == 1


def test_business_release_with_unknown_new_input_does_not_silently_run_full(repository):
    from tools.submit import RELEASE_BRANCH
    from tools.submit_validation import ReuseUnavailable
    repo, remote = repository
    api, commands, modes = FakeGitHub(repo, remote), [], []
    runner = release_runner(repo, api, commands)
    def unexpected_release_input(command):
        runner(command)
        if command == "release-pr":
            pull = api.pulls[2]
            old_head = pull["head"]["sha"]
            blob = git_input(repo, ["hash-object", "-w", "--stdin"], "unknown\n")
            entries = git(repo, "ls-tree", old_head).splitlines()
            entries.append(f"100644 blob {blob}\tunclassified-input.bin")
            tree = git_input(repo, ["mktree"], "\n".join(entries) + "\n")
            head = git(repo, "commit-tree", tree, "-p", old_head, "-m", "unexpected input")
            git(repo, "push", "origin", head + ":refs/heads/" + RELEASE_BRANCH)
            api.upsert_pull(RELEASE_BRANCH, pull["title"], pull["body"], head)
    unexpected_release_input.preview = runner.preview
    def reject(*args, **kwargs):
        mode = kwargs.get("mode", "full")
        modes.append(mode)
        if mode == "release":
            raise ReuseUnavailable("Unexpected release difference")
        return validate(*args, **kwargs)
    with pytest.raises(SubmitError, match="unclassified-input.bin"):
        run(repo, api, validate=reject, release_cli=unexpected_release_input)
    assert modes == ["planned"]
    assert len(api.merges) == 1


def test_published_release_can_recover_after_main_advances(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    runner = release_runner(repo, api, commands, fail_after_publish=True)
    with pytest.raises(SubmitError, match="publish response lost"):
        run(repo, api, release_cli=runner)
    new_main = git(repo, "commit-tree", git(repo, "rev-parse", api.main_sha() + "^{tree}"),
                   "-p", api.main_sha(), "-m", "docs: 发布后的说明")
    git(repo, "push", "origin", new_main + ":refs/heads/main")
    assert run(repo, api, release_cli=runner)["status"] == "complete"
    assert commands == ["release-pr", "github-release"]


def test_release_title_must_match_candidate_version_before_merge(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    runner = release_runner(repo, api, commands)
    def wrong_title(command):
        runner(command)
        api.pulls[2]["title"] = "chore: 发布 9.9.9"
    with pytest.raises(SubmitError, match="version.*title"):
        run(repo, api, release_cli=wrong_title)
    assert len(api.merges) == 1


def test_actual_release_test_failure_does_not_retry_full_suite(repository):
    from tools.submit_validation import ValidationError
    repo, remote = repository
    api, commands, modes = FakeGitHub(repo, remote), [], []
    def failing_tests(*args, **kwargs):
        modes.append(kwargs.get("mode", "full"))
        if kwargs.get("mode") == "release":
            raise ValidationError("release test failed")
        return validate(*args, **kwargs)
    with pytest.raises(ValidationError, match="release test failed"):
        run(repo, api, validate=failing_tests, release_cli=release_runner(repo, api, commands))
    assert modes == ["planned", "release"]
    assert len(api.merges) == 1


def test_release_candidate_cannot_decrease_product_version(repository):
    repo, remote = repository
    (repo / "VERSION").write_text("2.0.0 # x-release-please-version\n")
    git(repo, "commit", "-am", "chore: 合成版本基线")
    api, commands = FakeGitHub(repo, remote), []
    with pytest.raises(SubmitError, match="increase"):
        run(repo, api, release_cli=release_runner(repo, api, commands))
    assert len(api.merges) == 1


def test_cli_dry_run_prints_plan_without_credentials(repository, tmp_path, monkeypatch, capsys):
    import tools.submit as module
    repo, remote = repository
    git(repo, "remote", "set-url", "origin", "https://github.com/skuyd/emr.git")
    body = tmp_path / "body.txt"
    body.write_text("测试正文", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setattr(module, "validation_plan_for", lambda *_: recommended_plan(repo))
    def no_credentials(*args):
        raise AssertionError("dry-run must not load credentials")
    monkeypatch.setattr(module, "credentials", no_credentials)
    assert module.main(["--title", "feat: 新增示例", "--body-file", str(body), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "dry-run" in output and "Plan:" in output and "github-release" in output
    assert "tests/app/test_example.py" in output
    assert "python tools/run_required_tests.py" in output
    assert "Other Django modules" in output
    assert module.validation_plan_digest(recommended_plan(repo)) in output


def test_script_uses_its_worktree_validator_with_another_checkout_on_pythonpath(repository, tmp_path):
    repo, _ = repository
    stale = tmp_path / "another-checkout"
    (stale / "tools").mkdir(parents=True)
    (stale / "tools/__init__.py").write_text("")
    (stale / "tools/submit_validation.py").write_text("raise AssertionError('stale validator imported')\n")
    script = Path(__file__).resolve().parents[2] / "tools/submit.py"
    probe = '''import runpy, subprocess, sys
from pathlib import Path
module = runpy.run_path(sys.argv[1])
repo = Path(sys.argv[2])
def git(*args):
    return subprocess.check_output(['git', '-C', str(repo), *args]).decode().strip()
head, base = git('rev-parse', 'HEAD'), git('rev-parse', 'origin/main')
def validate(repo, revision, state_dir, **kwargs):
    assert kwargs == {'mode': 'planned', 'base_revision': base}
    return {'status': 'passed', 'revision': revision, 'tree': git('rev-parse', revision + '^{tree}'),
            'receipt_path': 'synthetic-receipt.json'}
module['feature_receipt_for'](repo, {'head': head, 'base': base}, repo, validate)
print('worktree validator used')
'''
    result = subprocess.run([sys.executable, "-c", probe, str(script), str(repo)],
                            cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(stale)),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "worktree validator used" in result.stdout


def test_unpublished_release_recovers_after_main_advances_with_precise_preview(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    runner = release_runner(repo, api, commands)
    def interrupt(command):
        if command == "github-release":
            raise SubmitError("interrupted before publish")
        runner(command)
    interrupt.preview = runner.preview
    with pytest.raises(SubmitError, match="interrupted before publish"):
        run(repo, api, release_cli=interrupt)
    release_sha = api.main_sha()
    new_main = git(repo, "commit-tree", git(repo, "rev-parse", release_sha + "^{tree}"),
                   "-p", release_sha, "-m", "docs: 继续说明")
    git(repo, "push", "origin", new_main + ":refs/heads/main")
    # The official release uses the merged release PR SHA, not the current tip.
    def publish(command):
        assert command == "github-release"
        api.tags["v1.1.0"] = release_sha
        api.releases["v1.1.0"] = {"tag_name": "v1.1.0", "draft": False, "html_url": "release"}
    publish.preview = runner.preview
    assert run(repo, api, release_cli=publish)["status"] == "complete"
    assert api.tags["v1.1.0"] == release_sha


def test_unexpected_pending_release_prevents_publishing(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    runner = release_runner(repo, api, commands)
    runner.preview = lambda: [{"pr": 999, "tag": "v9.9.9", "sha": "a" * 40}]
    with pytest.raises(SubmitError, match="pending release"):
        run(repo, api, release_cli=runner)
    assert commands == ["release-pr"]


def test_published_release_recovers_release_please_pending_label(repository):
    repo, remote = repository
    api, commands = FakeGitHub(repo, remote), []
    runner = release_runner(repo, api, commands, fail_after_publish=True)
    with pytest.raises(SubmitError, match="publish response lost"):
        run(repo, api, release_cli=runner)
    assert api.pulls[2]["labels"] == [{"name": "autorelease: pending"}]
    assert run(repo, api, release_cli=runner)["status"] == "complete"
    assert api.label_updates == [2]
    assert api.pulls[2]["labels"] == [{"name": "autorelease: tagged"}]


def test_no_open_release_is_not_success_when_an_old_merged_release_is_pending(repository):
    from tools.submit import RELEASE_BRANCH
    repo, remote = repository
    api = FakeGitHub(repo, remote)
    def blocked_release(command):
        pending = api.upsert_pull(RELEASE_BRANCH, "chore: 发布 1.0.0", "旧发布", api.main_sha())
        api.pulls[pending["number"]].update(merged=True, state="closed")
    with pytest.raises(SubmitError, match="merged.*pending"):
        run(repo, api, release_cli=blocked_release)


def test_github_pr_listing_paginates_without_per_pr_detail_requests():
    from tools.submit import GitHub
    from urllib.parse import parse_qs, urlsplit
    api = GitHub("synthetic-test-token")
    calls = []
    def request(endpoint):
        calls.append(endpoint)
        assert endpoint.startswith("pulls?")
        page = int(parse_qs(urlsplit(endpoint).query)["page"][0])
        return [{"number": number, "merged_at": None} for number in (range(1, 101) if page == 1 else [101])]
    api.request = request
    pulls = api.find_pulls("release-please--branches--main--components--family-phr")
    assert len(pulls) == 101
    assert len(calls) == 2
    assert all(pull["merged"] is False for pull in pulls)


def test_github_pr_listing_retains_candidate_fields_and_normalizes_merged_at():
    from tools.submit import GitHub
    api = GitHub("synthetic-test-token")
    pull = {"number": 42, "merged_at": "2026-09-26T00:00:00Z", "state": "closed",
            "head": {"sha": "a" * 40}, "base": {"ref": "main"},
            "labels": [{"name": "autorelease: pending"}], "title": "chore: 发布 1.1.0"}
    def request(endpoint):
        assert endpoint.startswith("pulls?")
        return [pull]
    api.request = request
    assert api.find_pulls("release-branch") == [{**pull, "merged": True}]
