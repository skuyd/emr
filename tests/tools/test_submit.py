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
    (repo / "app.txt").write_text("base\n")
    (repo / "VERSION").write_text("1.0.0 # x-release-please-version\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "chore: 初始化")
    git(repo, "push", "origin", "main")
    git(repo, "checkout", "-b", "feat/example")
    (repo / "app.txt").write_text("feature\n")
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


def release_runner(repo, api, commands, *, fail_after_publish=False):
    from tools.submit import RELEASE_BRANCH
    def execute(command):
        commands.append(command)
        if command == "release-pr":
            blob = git_input(repo, ["hash-object", "-w", "--stdin"], "1.1.0 # x-release-please-version\n")
            entries = git(repo, "ls-tree", api.main_sha()).splitlines()
            entries = [f"100644 blob {blob}\tVERSION" if line.endswith("\tVERSION") else line for line in entries]
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
    (repo / "app.txt").write_text("fixed feature\n")
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


def test_docs_only_candidate_selects_documentation_validation(repository):
    repo, remote = repository
    git(repo, "restore", "--source=origin/main", "app.txt")
    (repo / "docs").mkdir()
    (repo / "docs/README.md").write_text("Documentation update\n")
    git(repo, "add", "app.txt", "docs/README.md")
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


def test_release_reuse_rejection_falls_back_to_full_validation(repository):
    from tools.submit_validation import ReuseUnavailable
    repo, remote = repository
    api, commands, modes = FakeGitHub(repo, remote), [], []
    def changed_environment(*args, **kwargs):
        mode = kwargs.get("mode", "full")
        modes.append(mode)
        if mode == "release":
            raise ReuseUnavailable("Release baseline environment does not match")
        return validate(*args, **kwargs)
    result = run(repo, api, validate=changed_environment, release_cli=release_runner(repo, api, commands))
    assert result["status"] == "complete"
    assert modes == ["full", "release", "full"]


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
    assert modes == ["full", "release"]
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
    def no_credentials(*args):
        raise AssertionError("dry-run must not load credentials")
    monkeypatch.setattr(module, "credentials", no_credentials)
    assert module.main(["--title", "feat: 新增示例", "--body-file", str(body), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "dry-run" in output and "Plan:" in output and "github-release" in output


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
    assert kwargs == {'mode': 'full'}
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
