"""Submit an already committed branch through locally verified Squash PRs.

The personal skill owns change selection and commits. This tool never checks out
main, force pushes, bypasses protection, or deploys. The lock covers cooperating
processes in this clone; GitHub cannot atomically compare both PR head and base.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    # Direct script execution must prefer this checkout over installed copies.
    sys.path.insert(0, str(ROOT))
REPOSITORY = "skuyd/emr"
RELEASE_BRANCH = "release-please--branches--main--components--family-phr"
SHA = re.compile(r"[0-9a-f]{40}")


class SubmitError(ValueError):
    pass


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    if result.returncode:
        # Git's diagnostics may contain credential-bearing remote URLs.
        raise SubmitError(f"Git {args[0]} failed; inspect repository state before retrying")
    return result.stdout.strip()


def common_dir(repo):
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


class SessionLock(AbstractContextManager):
    """OS lock, automatically released after a crash; shared by all worktrees."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise SubmitError("another submit holds this repository's session lock") from error
        return self

    def __exit__(self, *exc):
        if self.stream:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def save_state(path, state):
    descriptor, name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def credentials(repo):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(["git", "-C", str(repo), "credential", "fill"],
                            input="protocol=https\nhost=github.com\npath=skuyd/emr.git\n\n",
                            text=True, capture_output=True, env=env)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not fields.get("password"):
        raise SubmitError("GitHub credentials unavailable; configure GH_TOKEN or Git credential manager")
    return fields["password"]


class GitHub:
    repository = REPOSITORY

    def __init__(self, token):
        self.token = token

    def request(self, endpoint, *, method="GET", payload=None, missing_ok=False):
        request = Request(f"https://api.github.com/repos/{self.repository}/{endpoint}",
                          method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28",
                                   "User-Agent": "family-phr-local-submit"})
        try:
            with urlopen(request, timeout=45) as response:
                if response.status == 204:
                    return None
                return json.load(response)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise SubmitError(f"GitHub API HTTP {error.code}; no bypass attempted; retry reconciles remote state") from error
        except (URLError, TimeoutError) as error:
            raise SubmitError("GitHub response unavailable; retry to reconcile remote state") from error

    def main_sha(self):
        return self.request("git/ref/heads/main")["object"]["sha"]

    def actions_disabled(self):
        return self.request("actions/permissions").get("enabled") is False

    def pull(self, number):
        return self.request(f"pulls/{number}")

    def find_pulls(self, branch):
        pulls = {}
        page = 1
        while True:
            query = urlencode({"head": f"{self.repository.split('/')[0]}:{branch}",
                               "base": "main", "state": "all", "per_page": 100, "page": page})
            result = self.request("pulls?" + query)
            for pull in result:
                pulls[pull["number"]] = {**pull, "merged": bool(pull.get("merged_at"))}
            if len(result) < 100:
                return list(pulls.values())
            page += 1

    def upsert_pull(self, branch, title, body, head):
        opened = [p for p in self.find_pulls(branch) if p["state"] == "open"]
        if len(opened) > 1:
            raise SubmitError("More than one open PR for this branch")
        if opened:
            return self.request(f"pulls/{opened[0]['number']}", method="PATCH",
                                payload={"title": title, "body": body})
        return self.request("pulls", method="POST", payload={"head": branch, "base": "main",
                                                              "title": title, "body": body})

    def merge(self, number, head, title, body):
        result = self.request(f"pulls/{number}/merge", method="PUT",
                              payload={"sha": head, "merge_method": "squash",
                                       "commit_title": title, "commit_message": body})
        if not result.get("merged") or not result.get("sha"):
            raise SubmitError("GitHub refused the Squash merge; protection was not bypassed")
        return result["sha"]

    def tag_sha(self, tag):
        ref = self.request("git/ref/tags/" + quote(tag, safe=""), missing_ok=True)
        if ref is None:
            return None
        obj = ref["object"]
        # Support both lightweight and annotated tags, never rewrite either.
        for _ in range(5):
            if obj["type"] == "commit":
                return obj["sha"]
            if obj["type"] != "tag":
                break
            obj = self.request("git/tags/" + obj["sha"])["object"]
        raise SubmitError("Release tag does not resolve to a commit")

    def release(self, tag):
        return self.request("releases/tags/" + quote(tag, safe=""), missing_ok=True)

    def mark_released(self, number):
        # Keep all unrelated labels. Add tagged before removing pending so a
        # partial failure remains recoverable without pretending no release ran.
        self.request(f"issues/{number}/labels", method="POST", payload={"labels": ["autorelease: tagged"]})
        self.request(f"issues/{number}/labels/" + quote("autorelease: pending", safe=""),
                     method="DELETE", missing_ok=True)


class ReleasePlease:
    def __init__(self, repo, token):
        self.repo, self.token = Path(repo), token

    def __call__(self, command):
        cli = self.repo / "node_modules/release-please/build/src/bin/release-please.js"
        if not cli.is_file():
            raise SubmitError("Pinned Release Please is missing; run npm ci first")
        # 17.6.0 exports its parser but does not enable env parsing by default.
        # Fixed bootstrap enables the official token option via environment only.
        bootstrap = ("const c=require(process.argv[1]);"
                     "c.parser.env('RELEASE_PLEASE').parseAsync(process.argv.slice(2))"
                     ".catch(()=>{process.exitCode=1});")
        env = dict(os.environ, RELEASE_PLEASE_TOKEN=self.token)
        result = subprocess.run(["node", "-e", bootstrap, str(cli), command,
                                 "--repo-url", REPOSITORY, "--target-branch", "main",
                                 "--config-file", "release-please-config.json",
                                 "--manifest-file", ".release-please-manifest.json"],
                                cwd=self.repo, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        # Do not log CLI output: upstream diagnostics can include request objects.
        if result.returncode:
            raise SubmitError(f"Release Please {command} failed; retry reconciles PR/tag/Release facts")

    def preview(self):
        """Read the same pinned engine's pending releases before allowing writes."""
        bootstrap = (
            "const r=require(process.argv[1]);"
            "r.setLogger({info(){},warn(){},error(){},debug(){},trace(){}});"
            "(async()=>{const g=await r.GitHub.create({owner:'skuyd',repo:'emr',"
            "token:process.env.RELEASE_PLEASE_TOKEN});"
            "const m=await r.Manifest.fromManifest(g,'main','release-please-config.json',"
            "'.release-please-manifest.json');const a=await m.buildReleases();"
            "console.log(JSON.stringify(a.map(x=>({pr:x.pullRequest.number,"
            "tag:x.tag.toString(),sha:x.sha,draft:!!x.draft,prerelease:!!x.prerelease,"
            "force_tag:!!x.forceTag}))));})().catch(()=>{process.exitCode=1});"
        )
        result = subprocess.run(["node", "-e", bootstrap, str(self.repo / "node_modules/release-please")],
                                cwd=self.repo, env=dict(os.environ, RELEASE_PLEASE_TOKEN=self.token),
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise SubmitError("Release Please pending release preflight failed")
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise SubmitError("Release Please returned invalid pending release metadata") from error


def check_title(title, body):
    try:
        from tools.check_conventional_commit import TITLE_PATTERN, HAN_PATTERN
    except ModuleNotFoundError:
        from check_conventional_commit import TITLE_PATTERN, HAN_PATTERN
    match = TITLE_PATTERN.fullmatch(title)
    if match is None or not HAN_PATTERN.search(match.group("description")):
        raise SubmitError("PR title must use Conventional Commits with a Chinese description")


def require_main(api, expected):
    if api.main_sha() != expected:
        raise SubmitError("main changed; synchronize the candidate and run validation again")


def require_actions_disabled(api):
    if not api.actions_disabled():
        raise SubmitError("Repository Actions must be disabled before submit can change the remote")


def fetch_main(repo):
    git(repo, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    return git(repo, "rev-parse", "refs/remotes/origin/main")


def check_private_history(repo, base, head):
    paths = git(repo, "log", "--format=", "--name-only", "-z", base + ".." + head)
    if any(path.strip().startswith("docs/deployment/local/tencent-cloud/")
           for path in paths.split("\0")):
        raise SubmitError("Private deployment content occurs in new commit history; remove it from history before pushing")


def receipt_for(repo, revision, state_dir, validate, **kwargs):
    print(f"Validating {revision[:12]}: mode={kwargs.get('mode', 'full')}", flush=True)
    receipt = validate(repo, revision, state_dir, **kwargs)
    tree = git(repo, "rev-parse", revision + "^{tree}")
    if (receipt.get("status") != "passed" or receipt.get("revision") != revision
            or receipt.get("tree") != tree or not receipt.get("receipt_path")):
        raise SubmitError("Validation receipt does not prove the exact candidate passed")
    return receipt


def feature_receipt_for(repo, feature, state_dir, validate):
    try:
        from tools.submit_validation import select_validation_mode
    except ModuleNotFoundError:
        from submit_validation import select_validation_mode
    mode = select_validation_mode(repo, feature["base"], feature["head"])
    options = {"mode": mode}
    if mode == "docs":
        options["base_revision"] = feature["base"]
    return receipt_for(repo, feature["head"], state_dir, validate, **options)


def candidate(pull, branch, head, base, title, body):
    if (pull.get("draft") or pull.get("state") != "open"
            or pull["head"].get("repo", {}).get("full_name") != REPOSITORY
            or pull["head"]["ref"] != branch or pull["head"]["sha"] != head
            or pull["base"]["ref"] != "main" or pull["base"]["sha"] != base
            or pull["title"] != title or (pull.get("body") or "") != body):
        raise SubmitError("PR candidate changed or is not from the expected repository")


def verify_merged(repo, api, record):
    pull = api.pull(record["pr"])
    if (not pull.get("merged") or pull["head"]["sha"] != record["head"]
            or pull["head"].get("repo", {}).get("full_name") != REPOSITORY
            or pull["title"] != record["title"] or (pull.get("body") or "") != record["body"]):
        raise SubmitError("Merged PR does not match the recorded validated candidate")
    sha = pull.get("merge_commit_sha", "")
    if not SHA.fullmatch(sha):
        raise SubmitError("GitHub has not confirmed the merge commit")
    fetch_main(repo)
    if git(repo, "rev-parse", sha + "^{tree}") != record["receipt"]["tree"]:
        raise SubmitError("Merged tree differs from validated candidate; stop before release")
    record["merged"] = sha
    return sha


def merge_record(repo, api, record, state, path):
    pull = api.pull(record["pr"])
    if not pull.get("merged"):
        require_actions_disabled(api)
        require_main(api, record["base"])
        candidate(pull, record["branch"], record["head"], record["base"],
                  record["title"], record["body"])
        # State precedes mutation, so a lost response can be reconciled safely.
        save_state(path, state)
        api.merge(record["pr"], record["head"], record["title"], record["body"])
    sha = verify_merged(repo, api, record)
    save_state(path, state)
    return sha


def submit(repo, branch, title, body, *, api=None, validate=None, release_cli=None,
           dry_run=False, expected_origin=REPOSITORY):
    repo = Path(repo).resolve()
    check_title(title, body)
    git(repo, "check-ref-format", "--branch", branch)
    if branch in ("main", RELEASE_BRANCH) or branch.startswith("-"):
        raise SubmitError("submit requires a feature branch")
    if git(repo, "status", "--porcelain", "--untracked-files=normal"):
        raise SubmitError("submit requires a clean worktree; commit only this task's changes first")
    origin = git(repo, "remote", "get-url", "origin")
    if expected_origin and origin not in (f"https://github.com/{expected_origin}.git",
                                         f"https://github.com/{expected_origin}",
                                         f"git@github.com:{expected_origin}.git"):
        raise SubmitError("origin is not the expected GitHub repository")
    head = git(repo, "rev-parse", "refs/heads/" + branch)
    if dry_run:
        return {"status": "dry-run", "branch": branch, "head": head,
                "steps": ["validate feature", "push PR", "Squash", "release-pr",
                          "validate release candidate", "Squash", "github-release"]}
    directory = common_dir(repo) / "local-submit"
    with SessionLock(directory / "session.lock"):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(branch.encode()).hexdigest() + ".json")
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if api is None:
            token = credentials(repo)
            api = GitHub(token)
            release_cli = ReleasePlease(repo, token)
        if state and (state["branch"] != branch or state["head"] != head
                      or state["title"] != title or state["body"] != body):
            if state.get("status") != "complete":
                previous = state["feature"]
                if (state.get("release") is not None or previous.get("merged")
                        or ("pr" in previous and api.pull(previous["pr"]).get("merged"))):
                    raise SubmitError("Incomplete merged submit belongs to different content/title/body; finish its release first")
                # A corrected unmerged branch starts a fresh validation session.
                # Existing PRs are reconciled by branch and updated normally.
            state = {}
        if validate is None:
            try:
                from tools.submit_validation import validate_revision
            except ModuleNotFoundError:
                from submit_validation import validate_revision
            validate = validate_revision
        if release_cli is None:
            raise SubmitError("Release Please command runner is unavailable")
        require_actions_disabled(api)
        if not state:
            base = fetch_main(repo)
            require_main(api, base)
            if git(repo, "merge-base", base, head) != base:
                raise SubmitError("Feature branch must contain current main; synchronize and validate again")
            state = {"schema": 1, "branch": branch, "head": head, "title": title,
                     "body": body, "status": "in_progress", "release": None,
                     "feature": {"branch": branch, "head": head, "base": base,
                                 "title": title, "body": body}}
            save_state(path, state)
        feature = state["feature"]
        validated_now = False
        if "pr" not in feature:
            require_main(api, feature["base"])
            check_private_history(repo, feature["base"], head)
            feature["receipt"] = feature_receipt_for(repo, feature, directory, validate)
            validated_now = True
            save_state(path, state)
            require_actions_disabled(api)
            require_main(api, feature["base"])
            git(repo, "push", "origin", head + ":refs/heads/" + branch)
            pull = api.upsert_pull(branch, title, body, head)
            feature["pr"] = pull["number"]
            save_state(path, state)
        if not validated_now and not api.pull(feature["pr"]).get("merged"):
            require_main(api, feature["base"])
            candidate(api.pull(feature["pr"]), branch, head, feature["base"], title, body)
            # Re-enter the validator so its environment/dependency fingerprint can
            # reject stale evidence; it may reuse an exactly matching receipt.
            feature["receipt"] = feature_receipt_for(repo, feature, directory, validate)
            save_state(path, state)
        merge_record(repo, api, feature, state, path)
        if state.get("status") == "complete":
            if state["release"] is not None:
                finish_release(api, state["release"])
            return state
        release = state["release"]
        release_merged = release is not None and api.pull(release["pr"]).get("merged")
        if not release_merged and (release is None or api.main_sha() != release["base"]):
            base = fetch_main(repo)
            require_main(api, base)
            state["release_baseline"] = feature["receipt"]
            # A newer main invalidates the old candidate. Preserve the merged
            # feature receipt, then validate only the refreshed release candidate.
            state["release"] = None
            save_state(path, state)
            require_main(api, base)
            require_actions_disabled(api)
            release_cli("release-pr")
            require_main(api, base)
            release_pulls = api.find_pulls(RELEASE_BRANCH)
            pulls = [p for p in release_pulls if p["state"] == "open"]
            if len(pulls) > 1:
                raise SubmitError("Multiple release candidates need reconciliation")
            if not pulls:
                pending = [p for p in release_pulls if p.get("merged") and "autorelease: pending" in label_names(p)]
                if pending:
                    raise SubmitError(f"Release Please is blocked by merged pending PR #{pending[0]['number']}; resume that submit first")
                state["status"] = "complete"
                save_state(path, state)
                return state
            pull = pulls[0]
            match = re.fullmatch(r"chore: 发布 (\d+\.\d+\.\d+)", pull["title"])
            if not match:
                raise SubmitError("Unexpected Release Please candidate title")
            release = {"branch": RELEASE_BRANCH, "head": pull["head"]["sha"],
                       "base": base, "title": pull["title"],
                       "body": pull.get("body") or "", "pr": pull["number"],
                       "tag": "v" + match.group(1)}
            candidate(pull, RELEASE_BRANCH, release["head"], base,
                      release["title"], release["body"])
            state["release"] = release
            save_state(path, state)
        pull = api.pull(release["pr"])
        if not pull.get("merged"):
            require_main(api, release["base"])
            candidate(pull, RELEASE_BRANCH, release["head"], release["base"],
                      release["title"], release["body"])
            git(repo, "fetch", "--no-tags", "origin", "refs/heads/" + RELEASE_BRANCH)
            if git(repo, "rev-parse", "FETCH_HEAD") != release["head"]:
                raise SubmitError("Release branch head changed before local validation")
            if git(repo, "merge-base", release["base"], release["head"]) != release["base"]:
                raise SubmitError("Release candidate does not contain current main")
            version = git(repo, "show", release["head"] + ":VERSION").removesuffix(" # x-release-please-version")
            if "v" + version != release["tag"]:
                raise SubmitError("Release source version does not match its PR title")
            previous_version = git(repo, "show", release["base"] + ":VERSION").removesuffix(" # x-release-please-version")
            semver = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
            if (not re.fullmatch(semver, version) or not re.fullmatch(semver, previous_version)
                    or tuple(map(int, version.split("."))) <= tuple(map(int, previous_version.split(".")))):
                raise SubmitError("Release source version must increase from its main baseline")
            try:
                from tools.submit_validation import ReuseUnavailable
            except ModuleNotFoundError:
                from submit_validation import ReuseUnavailable
            try:
                release["receipt"] = receipt_for(repo, release["head"], directory, validate,
                    mode="release", baseline_receipt=state.get("release_baseline", feature["receipt"])["receipt_path"])
            except ReuseUnavailable as error:
                # A changed environment or non-version diff cannot reuse business
                # tests. It must pass the complete gate instead of getting stuck.
                release["validation_reason"] = str(error)
                print(f"Release requires full validation: {error}", flush=True)
                release["receipt"] = receipt_for(repo, release["head"], directory, validate)
                release["validation_mode"] = "full"
            save_state(path, state)
        if "receipt" not in release:
            raise SubmitError("Release PR merged without a recorded validation receipt")
        release_merge = merge_record(repo, api, release, state, path)
        existing_tag = api.tag_sha(release["tag"])
        if existing_tag is not None and existing_tag != release_merge:
            raise SubmitError("Existing version tag points elsewhere; tags are never overwritten")
        if api.release(release["tag"]) is None:
            current_main = fetch_main(repo)
            require_main(api, current_main)
            current_version = git(repo, "show", current_main + ":VERSION").removesuffix(" # x-release-please-version")
            if (git(repo, "merge-base", release_merge, current_main) != release_merge
                    or "v" + current_version != release["tag"]):
                raise SubmitError("main no longer descends from this release with the same version; reconcile publishing first")
            expected = [{"pr": release["pr"], "tag": release["tag"], "sha": release_merge,
                         "draft": False, "prerelease": False, "force_tag": False}]
            if not hasattr(release_cli, "preview") or release_cli.preview() != expected:
                raise SubmitError("Release Please pending release candidates differ from the validated PR/tag/SHA")
            require_main(api, current_main)
            require_actions_disabled(api)
            release_cli("github-release")
        finish_release(api, release)
        state["status"] = "complete"
        save_state(path, state)
        return state


def verify_release(api, release):
    if api.tag_sha(release["tag"]) != release["merged"]:
        raise SubmitError("Release tag does not point to the validated Squash commit")
    published = api.release(release["tag"])
    if (not published or published.get("draft") or published.get("tag_name") != release["tag"]):
        raise SubmitError("GitHub Release is not confirmed published; retry to reconcile")
    release["url"] = published["html_url"]


def label_names(pull):
    return {label["name"] for label in pull.get("labels", [])}


def finish_release(api, release):
    verify_release(api, release)
    pull = api.pull(release["pr"])
    if not pull.get("merged") or pull.get("merge_commit_sha") != release["merged"]:
        raise SubmitError("Published release PR identity changed; labels were not modified")
    labels = label_names(pull)
    if "autorelease: pending" in labels or "autorelease: tagged" not in labels:
        require_actions_disabled(api)
        api.mark_released(release["pr"])
        labels = label_names(api.pull(release["pr"]))
        if "autorelease: pending" in labels or "autorelease: tagged" not in labels:
            raise SubmitError("Published release labels are not reconciled; retry this submit")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        branch = args.branch or git(ROOT, "branch", "--show-current")
        result = submit(ROOT, branch, args.title, args.body_file.read_text(encoding="utf-8"),
                        dry_run=args.dry_run)
    except (SubmitError, OSError, ValueError, RuntimeError) as error:
        print(f"Submit stopped: {error}", file=sys.stderr)
        return 1
    # Only deliberately public identifiers; never dump the complete state/body.
    print(json.dumps({k: result.get(k) for k in ("status", "branch", "head")}, ensure_ascii=False))
    if result.get("steps"):
        print("Plan: " + " -> ".join(result["steps"]))
    if result.get("feature", {}).get("merged"):
        print(f"Feature PR: https://github.com/{REPOSITORY}/pull/{result['feature']['pr']}")
        print(f"Feature Squash: {result['feature']['merged']}")
    if result.get("release"):
        print(f"Version: {result['release']['tag'].removeprefix('v')}; tag: {result['release']['tag']}")
        print(f"Release PR: https://github.com/{REPOSITORY}/pull/{result['release']['pr']}")
        print(result["release"].get("url", "Release pending"))
    elif result.get("status") == "complete":
        print("No new release: Release Please found no releasable changes.")
    for stage in ("feature", "release"):
        receipt = (result.get(stage) or {}).get("receipt")
        if receipt:
            print(f"{stage} receipt: {receipt['receipt_path']}; mode={receipt.get('mode', 'full')}; "
                  f"reused={receipt.get('reused', False)}; "
                  f"business_reused={receipt.get('business_reused', False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
