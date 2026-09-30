"""Submit an already committed branch through locally verified Squash PRs.

The personal skill owns change selection and commits. This tool never checks out
main, overwrites remote commits, bypasses protection, or deploys. The lock covers cooperating
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


def require_origin_push_url(repo, origin):
    if git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines() != [origin]:
        raise SubmitError("origin must have one push URL matching the fetch URL used by submit")


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


def validation_plan_for(repo, base, head):
    try:
        from tools.submit_validation import select_validation_plan
    except ModuleNotFoundError:
        from submit_validation import select_validation_plan
    plan = select_validation_plan(repo, base, head)
    if plan["mode"] == "blocked":
        raise SubmitError("Validation scope needs clarification: " + plan["reason"])
    return plan


VALIDATION_TOOLS = ("tools/submit.py", "tools/submit_validation.py", "tools/local_validation.py")


def require_validation_tools(repo, candidate_head, *, feature_head=None):
    current_head = git(repo, "rev-parse", "HEAD")
    def entry(revision, name):
        return git(repo, "ls-tree", revision, "--", name)
    candidate = {name: entry(candidate_head, name) for name in VALIDATION_TOOLS}
    current_differences = [name for name in VALIDATION_TOOLS
                           if entry(current_head, name) != candidate[name]]
    feature_differences = []
    if feature_head is not None:
        feature_differences = [name for name in VALIDATION_TOOLS
                               if entry(feature_head, name) != candidate[name]]
    if current_differences:
        stage = "release" if feature_head is not None else "feature"
        raise SubmitError("Validation tools differ from " + stage + " candidate: "
                          + ", ".join(current_differences)
                          + "; synchronize the local checkout and review the plan again")
    if feature_differences:
        print("Using release candidate validation tools from the current checkout: "
              + ", ".join(feature_differences), flush=True)


def validation_plan_digest(plan, candidate_revision=None):
    source = ({"plan": plan, "candidate_revision": candidate_revision}
              if candidate_revision is not None and candidate_revision != plan["revision"] else plan)
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def print_validation_plan(plan, candidate_revision=None):
    print(f"Validation plan: {', '.join(plan['groups'])}; reason={plan['reason']}", flush=True)
    for kind, paths in plan.get("targets", {}).items():
        if paths:
            print(f"  {kind}: {', '.join(paths)}", flush=True)
    try:
        from tools.local_validation import command_catalog, validation_commands
    except ModuleNotFoundError:
        from local_validation import command_catalog, validation_commands
    if plan["mode"] == "docs":
        commands = validation_commands("docs")
    else:
        scoped = validation_commands("planned", plan["groups"], plan["targets"])
        catalog = command_catalog()
        commands = {group: scoped.get(group, catalog.get(group)) for group in plan["groups"]}
    print("  Planned commands:", flush=True)
    for group, command in commands.items():
        shown = "python " + " ".join(command) if isinstance(command, list) else command
        print(f"    {group}: {shown}", flush=True)
    if plan.get("full_recommended"):
        print(f"  Full suite recommended; plan digest: {validation_plan_digest(plan, candidate_revision)}", flush=True)
        catalog = command_catalog()
        print("  Full suite commands if selected:", flush=True)
        for group in validation_commands("full") | {key: catalog[key] for key in ("production-build", "production-smoke")}:
            print(f"    {group}: {catalog[group]}", flush=True)
    for risk in plan.get("risks", []):
        print(f"  Uncovered risk: {risk}", flush=True)


def validation_options(plan, record, full_tests, plan_digest, *, candidate_revision=None):
    options = {"mode": plan["mode"]}
    if plan["mode"] in {"docs", "planned"}:
        options["base_revision"] = plan["base_revision"]
    if not plan.get("full_recommended"):
        if full_tests is not None or plan_digest is not None:
            raise SubmitError("Full-test choice does not match the current plan; review the candidate again")
        return options
    digest = validation_plan_digest(plan, candidate_revision)
    previous = record.get("validation_decision", {})
    if full_tests is None and previous.get("plan_digest") == digest:
        full_tests = previous.get("choice")
        plan_digest = digest
    if full_tests not in {"run", "skip"} or plan_digest is None:
        raise SubmitError("Full validation is recommended. Review the plan, then pass "
                          "--full-tests run|skip --plan-digest <digest>")
    if plan_digest != digest:
        raise SubmitError("Validation plan digest changed; review the current candidate and choose again")
    record["validation_decision"] = {"choice": full_tests, "plan_digest": digest,
                                      "base": plan["base_revision"],
                                      "head": candidate_revision or plan["revision"]}
    options.update(mode="full" if full_tests == "run" else "planned",
                   base_revision=plan["base_revision"], scope_decision=full_tests,
                   plan_digest=digest)
    return options


def feature_receipt_for(repo, feature, state_dir, validate, *, full_tests=None, plan_digest=None):
    require_validation_tools(repo, feature["head"])
    plan = validation_plan_for(repo, feature["base"], feature["head"])
    feature["validation_plan"] = plan
    print_validation_plan(plan)
    options = validation_options(plan, feature, full_tests, plan_digest)
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
           dry_run=False, full_tests=None, plan_digest=None, expected_origin=REPOSITORY):
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
    require_origin_push_url(repo, origin)
    head = git(repo, "rev-parse", "refs/heads/" + branch)
    if dry_run:
        require_validation_tools(repo, head)
        return {"status": "dry-run", "branch": branch, "head": head,
                "validation_plan": validation_plan_for(repo, git(repo, "rev-parse", "origin/main"), head),
                "steps": ["validate feature", "push PR", "Squash", "release-pr",
                          "validate release candidate", "Squash", "github-release",
                          "clean up feature branch and worktree"]}
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
        state["origin"] = origin
        feature = state["feature"]
        validated_now = False
        if "pr" not in feature:
            require_main(api, feature["base"])
            check_private_history(repo, feature["base"], head)
            feature["receipt"] = feature_receipt_for(repo, feature, directory, validate,
                full_tests=full_tests, plan_digest=plan_digest)
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
            feature["receipt"] = feature_receipt_for(repo, feature, directory, validate,
                full_tests=full_tests, plan_digest=plan_digest)
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
            require_validation_tools(repo, release["head"], feature_head=feature["head"])
            try:
                from tools.submit_validation import ReuseUnavailable, ValidationError, _release_diff
            except ModuleNotFoundError:
                from submit_validation import ReuseUnavailable, ValidationError, _release_diff
            try:
                _release_diff(repo, release["base"], release["head"])
            except ValidationError as error:
                changed = git(repo, "diff", "--name-only", "--no-renames",
                              release["base"], release["head"])
                raise SubmitError("Release candidate has changes outside proven release metadata: "
                                  + changed.replace("\n", ", ") + "; " + str(error)) from error
            try:
                release["receipt"] = receipt_for(repo, release["head"], directory, validate,
                    mode="release", baseline_receipt=release.get("scope_receipt", state.get("release_baseline", feature["receipt"]))["receipt_path"])
            except ReuseUnavailable as error:
                release["validation_reason"] = str(error)
                plan = validation_plan_for(repo, feature["base"], release["base"])
                release["additional_validation_plan"] = plan
                save_state(path, state)
                print(f"Release baseline cannot be reused: {error}", flush=True)
                print_validation_plan(plan, release["head"])
                if plan["mode"] != "planned":
                    raise SubmitError("Release has no reusable business validation baseline; "
                                      "the current main difference contains documentation checks only")
                release_choice, release_digest = full_tests, plan_digest
                if feature.get("validation_decision") == {"choice": full_tests,
                        "plan_digest": plan_digest, "base": feature["base"], "head": feature["head"]}:
                    release_choice, release_digest = None, None
                options = validation_options(plan, release, release_choice, release_digest,
                                             candidate_revision=release["head"])
                options["plan_revision"] = release["base"]
                release["scope_receipt"] = receipt_for(repo, release["head"], directory,
                                                        validate, **options)
                save_state(path, state)
                release["receipt"] = receipt_for(repo, release["head"], directory, validate,
                    mode="release", baseline_receipt=release["scope_receipt"]["receipt_path"])
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


def worktree_records(repo):
    records = []
    for block in git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        record = {}
        for field in block.split("\0"):
            if field:
                name, _, value = field.partition(" ")
                record[name] = value
        if record:
            records.append(record)
    return records


def require_cleanup_worktree(primary, target, ref, head):
    records = worktree_records(primary)
    matches = [record for record in records if Path(record["worktree"]).absolute() == target]
    if len(matches) != 1 or matches[0].get("branch") != ref:
        raise SubmitError("Cleanup worktree branch changed; preserve it for inspection")
    resolved = target.resolve()
    if resolved == primary:
        raise SubmitError("The primary worktree is retained; switch it off the feature branch before cleanup")
    if Path(sys.executable).resolve().is_relative_to(resolved):
        raise SubmitError("Cleanup requires a Python interpreter outside the target worktree; "
                          "retry --cleanup-only with an external interpreter")
    shared = common_dir(primary)
    if (resolved != target or primary.is_relative_to(resolved) or shared.is_relative_to(resolved)
            or common_dir(target) != shared
            or any(Path(record["worktree"]).resolve().is_relative_to(resolved)
                   for record in records if Path(record["worktree"]).absolute() != target)):
        raise SubmitError("Cleanup worktree path is unsafe or contains another worktree")
    if "locked" in matches[0] or "prunable" in matches[0]:
        raise SubmitError("Cleanup worktree is locked or unavailable; preserve it for inspection")
    if git(target, "rev-parse", "HEAD") != head:
        raise SubmitError("Worktree head changed since the completed submit")
    if git(target, "status", "--porcelain", "--untracked-files=all"):
        raise SubmitError("Cleanup preserves uncommitted worktree changes; save them before retrying")
    index = git(target, "ls-files", "-v", "-z")
    if any(item and (item[0].islower() or item[0] == "S") for item in index.split("\0")):
        raise SubmitError("Cleanup preserves index flags that can hide local changes; "
                          "clear assume-unchanged/skip-worktree and inspect files before retrying")
    ignored = git(target, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    caches = {"node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    preserved = [name for name in ignored.split("\0") if name
                 and not caches.intersection(Path(name).parts)]
    if preserved:
        raise SubmitError("Cleanup preserves ignored local files; move private deployment material to "
                          "the primary worktree's docs/deployment/local/tencent-cloud/ and save other "
                          "local files before retrying: " + ", ".join(preserved[:10]))


def cleanup_completed_submit(repo, branch, *, expected_head=None):
    """Delete only the recorded, completed candidate; retain shared evidence."""
    repo = Path(repo).resolve()
    git(repo, "check-ref-format", "--branch", branch)
    if branch in ("main", RELEASE_BRANCH) or branch.startswith("-"):
        raise SubmitError("Cleanup requires a completed feature branch")
    directory = common_dir(repo) / "local-submit"
    with SessionLock(directory / "session.lock"):
        path = directory / (hashlib.sha256(branch.encode()).hexdigest() + ".json")
        if not path.is_file():
            raise SubmitError("No completed submit record for this branch")
        state = json.loads(path.read_text(encoding="utf-8"))
        feature = state.get("feature", {})
        head = state.get("head", "")
        if expected_head is not None and head != expected_head:
            raise SubmitError("Submit record changed before cleanup; preserve the later task")
        if (state.get("status") != "complete" or state.get("branch") != branch
                or feature.get("branch") != branch or feature.get("head") != head
                or not SHA.fullmatch(head) or not SHA.fullmatch(feature.get("merged", ""))):
            raise SubmitError("All submit stages must be completed before cleanup")
        release = state.get("release")
        if release is not None and (not release.get("url") or not SHA.fullmatch(release.get("merged", ""))):
            raise SubmitError("Release must be confirmed published before cleanup")
        cleanup = state.get("cleanup", {})
        if cleanup.get("status") == "complete":
            return state
        if state.get("origin") != git(repo, "remote", "get-url", "origin"):
            raise SubmitError("Cleanup origin differs from completed submit; reconcile using regular submit")
        require_origin_push_url(repo, state["origin"])
        main = fetch_main(repo)
        for record in (feature, release):
            if record is None:
                continue
            if (git(repo, "merge-base", record["merged"], main) != record["merged"]
                    or git(repo, "rev-parse", record["merged"] + "^{tree}")
                    != record.get("receipt", {}).get("tree")):
                raise SubmitError("Completed candidate is no longer confirmed on origin/main")

        records = worktree_records(repo)
        primary = Path(records[0]["worktree"]).resolve()
        if ("bare" in records[0] or common_dir(primary) != directory.parent
                or Path(git(primary, "rev-parse", "--path-format=absolute", "--git-dir")).resolve()
                != directory.parent):
            raise SubmitError("Cannot identify the primary worktree for safe cleanup")
        ref = "refs/heads/" + branch
        matches = [record for record in records if record.get("branch") == ref]
        if len(matches) > 1:
            raise SubmitError("Feature branch is checked out in multiple worktrees")
        target = Path(matches[0]["worktree"]).absolute() if matches else None
        if cleanup:
            recorded_target = Path(cleanup["worktree"]) if cleanup.get("worktree") else None
            if target != recorded_target and (target is not None or (recorded_target and recorded_target.exists())):
                raise SubmitError("Recorded cleanup worktree changed; preserve it for inspection")
        local_head = git(primary, "for-each-ref", "--format=%(objectname)", ref)
        if local_head != head and (local_head or not cleanup):
            raise SubmitError("Local branch head changed since the completed submit")
        if target is not None:
            require_cleanup_worktree(primary, target, ref, head)
        remote = git(repo, "ls-remote", "--heads", "origin", ref)
        if remote and remote.split() != [head, ref]:
            raise SubmitError("The remote branch head changed since the completed submit")
        if not cleanup:
            cleanup = state["cleanup"] = {"status": "pending", "worktree": str(target) if target else None}
            save_state(path, state)
        if remote:
            # Conditional deletion: a concurrent remote commit must win over cleanup.
            git(repo, "push", f"--force-with-lease={ref}:{head}", "--no-follow-tags", "origin", ":" + ref)
        if target is not None:
            require_cleanup_worktree(primary, target, ref, head)
            if Path.cwd().resolve().is_relative_to(target):
                os.chdir(primary)
            # No --force: Git independently refuses dirty, locked or nested repos.
            git(primary, "worktree", "remove", "--", str(target))
        if git(primary, "for-each-ref", "--format=%(objectname)", ref):
            if any(record.get("branch") == ref for record in worktree_records(primary)):
                raise SubmitError("Feature branch was checked out during cleanup; preserve it")
            # Squash commits have a different ancestry; compare the exact old ref.
            git(primary, "update-ref", "-d", ref, head)
        cleanup["status"] = "complete"
        save_state(path, state)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch")
    parser.add_argument("--title")
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true",
                        help="Retry cleanup of a completed submit without tests or release actions")
    parser.add_argument("--full-tests", choices=("run", "skip"),
                        help="Choice for the displayed full-suite recommendation")
    parser.add_argument("--plan-digest", help="Digest of the reviewed validation plan")
    args = parser.parse_args(argv)
    if args.cleanup_only:
        if not args.branch or args.dry_run or args.full_tests or args.plan_digest:
            parser.error("--cleanup-only requires --branch and cannot run validation or a dry-run")
    elif not args.title or args.body_file is None:
        parser.error("--title and --body-file are required for submit")
    try:
        branch = args.branch or git(ROOT, "branch", "--show-current")
        if args.cleanup_only:
            result = cleanup_completed_submit(ROOT, branch)
        else:
            result = submit(ROOT, branch, args.title, args.body_file.read_text(encoding="utf-8"),
                            dry_run=args.dry_run, full_tests=args.full_tests,
                            plan_digest=args.plan_digest)
    except (SubmitError, OSError, ValueError, RuntimeError) as error:
        print(f"Submit stopped: {error}", file=sys.stderr)
        return 1
    # Only deliberately public identifiers; never dump the complete state/body.
    print(json.dumps({k: result.get(k) for k in ("status", "branch", "head")}, ensure_ascii=False))
    if result.get("steps"):
        print("Plan: " + " -> ".join(result["steps"]))
        plan = result["validation_plan"]
        print_validation_plan(plan)
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
                  f"business_reused={receipt.get('business_reused', False)}; "
                  f"validation_reused={receipt.get('validation_reused', False)}")
            if receipt.get("scope_decision"):
                if receipt["scope_decision"] == "skip":
                    print(f"{stage} full-suite choice: skip; uncovered risks: "
                          f"{', '.join(receipt.get('risks', [])) or 'none'}")
                else:
                    print(f"{stage} full-suite choice: run; full business suite passed")
    if result.get("status") == "complete":
        if not args.cleanup_only:
            try:
                cleanup_completed_submit(ROOT, branch, expected_head=result["head"])
            except (SubmitError, OSError, ValueError, RuntimeError) as error:
                print(f"Submit completed; cleanup pending: {error}. Retry from a retained worktree with "
                      f"--cleanup-only --branch {branch}", file=sys.stderr)
                return 1
        print(f"Cleanup complete: local and remote branch {branch}; linked worktree removed if present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
