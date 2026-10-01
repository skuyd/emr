"""Validate an exact Git archive on Linux, without GitHub secrets."""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET


BASE_IMAGES = {"python": "python:3.11.16-slim-bookworm", "node": "node:22-bookworm-slim", "postgres": "postgres:18.6-alpine3.24"}
DEPENDENCY_FILES = ("requirements-prod.lock", "requirements-test.lock", "package.json", "package-lock.json", "deploy/local-validation.Dockerfile")
BROWSER_FILES = tuple("tests/browser/" + name for name in (
    "test_ac02_upload_browser.py", "test_upload_interactions_browser.py", "test_cloud_sources_browser.py",
    "test_cloud_open_browser.py", "test_cloud_selected_output_browser.py", "test_molecular_browser.py",
    "test_molecular_outputs_browser.py", "test_molecular_combined_browser.py",
))


WORKFLOW_COMMAND = "python -m pytest -q --durations=30 --junitxml=/evidence/workflow.xml tests/tools/test_submit.py tests/tools/test_submit_validation.py tests/tools/test_local_validation.py"
PRODUCTION_SMOKE = "import os; assert os.getuid() == 10001; import django, celery, pypdfium2; from apps.exports.files import private_temporary_file; output=private_temporary_file(); output.write(b'synthetic-export-probe'); output.close(); print('Production image imports and private export temp write verified')"
JUNIT_GROUPS = {"python", "browser", "postgres", "release-tests", "workflow"}
TARGET_GROUPS = ("python", "browser", "postgres", "javascript")
STATIC_DESIGN_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf")


def validate_targets(targets, groups, source=None):
    selected = set(groups) & set(TARGET_GROUPS)
    if targets is None:
        if selected:
            raise ValueError("Planned test groups require exact targets")
        return None
    if not isinstance(targets, dict) or set(targets) != set(TARGET_GROUPS):
        raise ValueError("Planned targets must name the four test groups")
    normalized = {}
    for group in TARGET_GROUPS:
        paths = targets[group]
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("Planned targets must be test path arrays")
        if len(paths) != len(set(paths)):
            raise ValueError("Planned targets must be unique")
        if bool(paths) != (group in selected):
            raise ValueError("Planned test groups and target paths differ")
        for name in paths:
            path = PurePosixPath(name)
            prototype_test = name.startswith("prototype-gallery/") and "/tests/" in name
            if group == "browser":
                allowed = name.startswith("tests/browser/")
            elif group == "javascript":
                allowed = name.startswith("tests/js/") or prototype_test
            else:
                allowed = name.startswith("tests/") or prototype_test
            extension = name.endswith(".test.mjs") if group == "javascript" else path.name.startswith("test_") and name.endswith(".py")
            if (not allowed or not extension or path.as_posix() != name
                    or any(part in {".", ".."} for part in path.parts)
                    or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
                    or (group == "python" and name.startswith("tests/browser/"))):
                raise ValueError("Unsafe or non-test planned target: " + name)
            if source is not None:
                target = source / name
                if not target.is_file() or target.is_symlink():
                    raise ValueError("Planned test target is missing or unsafe: " + name)
        normalized[group] = sorted(paths)
    return normalized


def scoped_test_command(group, paths):
    quoted = " ".join(shlex.quote(path) for path in paths)
    if group == "javascript":
        return "node --test --test-reporter=junit --test-reporter-destination=/evidence/javascript.xml " + quoted
    command = "python tools/run_required_tests.py -q --durations=30 --junitxml=/evidence/" + group + ".xml "
    if group == "python":
        command += '-m "not postgres and not ocr_model" '
    elif group == "postgres":
        command += "--ds=config.settings.postgres_test -m postgres "
    return command + quoted


def validation_commands(mode, groups=None, targets=None):
    if mode == "planned":
        selected = required_steps(mode, groups)
        targets = validate_targets(targets, selected)
        catalog = command_catalog()
        return {name: scoped_test_command(name, targets[name]) if name in TARGET_GROUPS else catalog[name]
                for name in selected if not name.startswith("production-")}
    if mode == "docs":
        return {
            "documentation": ["tools/verify_documentation.py"],
            "traceability": ["tools/verify_traceability.py"],
            "release-gate": ["tools/verify_release_gate.py"],
            "version": ["tools/release_version.py", "check"],
        }
    contracts = " && ".join("python " + command for command in (
        "tools/verify_release_automation.py", "tools/release_version.py check", "tools/verify_documentation.py",
        "tools/verify_traceability.py", "tools/verify_release_gate.py",
    ))
    commands = {"contracts": contracts}
    if mode == "full":
        commands.update({
            "django": "python manage.py check && python manage.py makemigrations --check --dry-run",
            "python": 'python -m pytest -q --durations=30 --junitxml=/evidence/python.xml -m "not postgres and not ocr_model" ' + " ".join("--ignore=" + name for name in BROWSER_FILES),
            "browser": "python tools/run_required_tests.py -q --durations=30 --junitxml=/evidence/browser.xml " + " ".join(BROWSER_FILES),
            "javascript": "npm run test:js",
            "postgres": "python tools/run_required_tests.py -q --durations=30 --junitxml=/evidence/postgres.xml --ds=config.settings.postgres_test -m postgres tests",
        })
    else:
        commands["release-tests"] = "python tools/run_required_tests.py -q --durations=30 --junitxml=/evidence/release-tests.xml tests/tools/test_release_version.py tests/tools/test_release_automation.py tests/tools/test_documentation.py tests/tools/test_dependency_locks.py tests/tools/test_verify_release_gate.py tests/deploy/test_release_artifacts.py tests/tools/test_phase_two_evaluation.py tests/labs/test_phase_two_release_evaluation.py"
    commands["corpus"] = "python tools/phase_two_evaluation.py --synthetic-only --report /evidence/phase-two-release-evaluation.json"
    return commands


def command_catalog():
    return {**validation_commands("full"), **validation_commands("release"), "workflow": WORKFLOW_COMMAND,
            "production-build": "docker build --file pinned-production.Dockerfile --tag production .",
            "production-smoke": PRODUCTION_SMOKE}


def required_steps(mode, groups=None):
    if mode == "planned":
        if (not isinstance(groups, list) or not groups or any(not isinstance(name, str) for name in groups)
                or len(groups) != len(set(groups)) or not set(groups) <= command_catalog().keys()):
            raise ValueError("Planned validation requires unique known groups")
        if ("production-build" in groups) != ("production-smoke" in groups):
            raise ValueError("Production build and smoke must be selected together")
        return list(groups)
    if mode == "docs":
        return list(validation_commands(mode))
    return [*validation_commands(mode), "production-build", "production-smoke"]


def dependency_key(source, base_ids):
    digest = hashlib.sha256(json.dumps(base_ids, sort_keys=True).encode())
    for name in DEPENDENCY_FILES:
        data = (source / name).read_bytes().replace(b"\r\n", b"\n")
        if name in {"package.json", "package-lock.json"}:
            value = json.loads(data)
            value.pop("version", None)
            value.get("packages", {}).get("", {}).pop("version", None)
            data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def clean_environment():
    # Allow only process/runtime plumbing; never forward inherited API credentials.
    allowed = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def run_step(name, command, output, *, cwd=None):
    output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    log = output / (name + ".log")
    try:
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(command, cwd=cwd, env=clean_environment(), stdout=stream, stderr=subprocess.STDOUT)
        code = result.returncode
    except OSError as error:
        log.write_text(str(error), encoding="utf-8")
        code = 1
    return {"name": name, "status": "passed" if code == 0 else "failed", "returncode": code,
            "seconds": round(time.monotonic() - start, 3), "log": log.name}


def require_test_report(report, *, allow_skips=False):
    try:
        cases = list(ET.parse(report).getroot().iter("testcase"))
    except (OSError, ET.ParseError) as error:
        raise ValueError("Missing or invalid mandatory test report") from error
    rejected = ("failure", "error") if allow_skips else ("skipped", "failure", "error")
    if not cases or any(any(case.find(tag) is not None for tag in rejected) for case in cases):
        raise ValueError("Mandatory tests were empty, skipped, or failed")
    return len(cases)


def extract_archive(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or any(part.lower() == ".git" for part in path.parts) or not (member.isfile() or member.isdir()):
                raise ValueError("Archive contains an unsafe path or link")
        stream.extractall(destination, members=members, filter="data")


def initialize_source_git(source, pack, revision):
    """Attach only the real candidate's objects and index to unchanged archive bytes."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("Source revision must be an exact Git object ID")
    metadata = source / ".git"
    if metadata.exists() or metadata.is_symlink():
        raise ValueError("Source must not supply Git metadata")
    environment = {**clean_environment(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    def git(*args, input=None):
        return subprocess.check_output(["git", "-C", str(source), *args], input=input,
                                       env=environment, stderr=subprocess.PIPE)
    try:
        with tempfile.TemporaryDirectory(prefix="empty-git-template-") as template:
            git("init", "-q", "--template=" + template,
                "--object-format=" + ("sha256" if len(revision) == 64 else "sha1"))
        git("config", "core.autocrlf", "false")
        git("config", "core.eol", "lf")
        git("index-pack", "--stdin", input=pack.read_bytes())
        if git("cat-file", "-t", revision).strip() != b"commit":
            raise ValueError("Source revision is not a commit")
        objects = {revision, git("rev-parse", revision + "^{tree}").decode().strip()}
        expected = {}
        for entry in git("ls-tree", "-rzt", revision).split(b"\0"):
            if not entry:
                continue
            info, name = entry.split(b"\t", 1)
            mode, kind, oid = info.decode().split()
            objects.add(oid)
            path = PurePosixPath(name.decode("utf-8"))
            if path.is_absolute() or ".." in path.parts or any(part.lower() == ".git" for part in path.parts):
                raise ValueError("Git tree contains an unsafe source path")
            if kind == "tree":
                continue
            if kind != "blob" or mode not in ("100644", "100755"):
                raise ValueError("Git tree contains an unsupported source entry")
            expected[path.as_posix()] = oid
            if os.name != "nt" and (source / path).is_file() and bool((source / path).stat().st_mode & 0o111) != (mode == "100755"):
                raise ValueError("Source executable mode differs from the Git candidate")
        actual_objects = set(git("cat-file", "--batch-all-objects", "--batch-check=%(objectname)").decode().splitlines())
        if actual_objects != objects:
            raise ValueError("Git pack contains missing or unrelated objects")
        actual = {}
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if relative.parts[0] == ".git":
                continue
            if path.is_symlink():
                raise ValueError("Source contains a symbolic link")
            if path.is_file():
                data = path.read_bytes()
                algorithm = "sha256" if len(revision) == 64 else "sha1"
                actual[relative.as_posix()] = hashlib.new(algorithm, f"blob {len(data)}\0".encode() + data).hexdigest()
        if actual != expected:
            raise ValueError("Source bytes differ from the exact Git candidate")
        (metadata / "shallow").write_text(revision + "\n", encoding="ascii")
        git("update-ref", "--no-deref", "HEAD", revision)
        git("read-tree", revision)
    except BaseException:
        if metadata.exists():
            if os.name == "nt":
                for path in metadata.rglob("*"):
                    if path.is_file():
                        path.chmod(0o600)
            shutil.rmtree(metadata)
        raise


def capture(command):
    return subprocess.check_output(command, env=clean_environment(), text=True, stderr=subprocess.PIPE).strip()


def image_id(reference):
    return capture(["docker", "image", "inspect", "--format", "{{.Id}}", reference])


def base_reference(reference):
    return json.loads(capture(["docker", "image", "inspect", reference]))[0]["RepoDigests"][0]


def pinned_production_recipe(recipe, reference):
    content = recipe.read_text(encoding="utf-8")
    expected = "FROM " + BASE_IMAGES["python"] + "\n"
    if not content.startswith(expected):
        raise ValueError("Production base differs from the validated Python base")
    return "FROM " + reference + "\n" + content[len(expected):]


def prepare_environment(source, cache, mode="full", groups=None, targets=None):
    selected = required_steps(mode, groups)
    if mode == "planned":
        targets = validate_targets(targets, selected, source)
    if mode == "docs":
        policy = {"docs": validation_commands("docs")}
        command_digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode() + Path(__file__).read_bytes()).hexdigest()
        return {"schema": 1, "command_digest": command_digest,
                "required_steps": {"docs": required_steps("docs")},
                "python": {"version": sys.version, "implementation": platform.python_implementation(),
                           "executable": sys.executable},
                "git": capture(["git", "--version"]),
                "architecture": platform.machine(), "kernel": platform.release()}
    base_ids = {}
    for name, reference in BASE_IMAGES.items():
        try:
            base_ids[name] = base_reference(reference)
        except subprocess.CalledProcessError:
            step = run_step("pull-" + name, ["docker", "pull", reference], cache)
            if step["status"] != "passed":
                raise RuntimeError("Base image download failed; see " + str(cache / step["log"]))
            base_ids[name] = base_reference(reference)
    key = dependency_key(source, base_ids)
    tag = "emr-local-validation:" + key
    try:
        dependency_id = image_id(tag)
    except subprocess.CalledProcessError:
        with tempfile.TemporaryDirectory(prefix="dependencies-", dir=cache) as directory:
            context = Path(directory)
            for name in DEPENDENCY_FILES:
                target = context / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            command = ["docker", "build", "--file", str(context / "deploy/local-validation.Dockerfile"), "--tag", tag,
                       "--output", "type=image,compression=uncompressed",
                       "--build-arg", "PYTHON_IMAGE=" + base_ids["python"], "--build-arg", "NODE_IMAGE=" + base_ids["node"], str(context)]
            step = run_step("dependency-build", command, cache)
            if step["status"] != "passed":
                raise RuntimeError("Dependency image build failed; see " + str(cache / step["log"]))
        dependency_id = image_id(tag)
    policy = command_catalog()
    if mode == "planned" and targets is not None:
        policy = {**policy, "planned": validation_commands("planned", groups, targets), "targets": targets}
    command_digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode() + Path(__file__).read_bytes()).hexdigest()
    steps = {mode: required_steps(mode) for mode in ("full", "release")}
    if mode == "planned":
        steps["planned"] = selected
    fingerprint = {"schema": 1, "command_digest": command_digest,
            "required_steps": steps,
            "dependency_key": key, "dependency_image": dependency_id, "base_images": base_ids,
            "docker": json.loads(capture(["docker", "version", "--format", "{{json .Server}}"]))["Version"],
            "architecture": platform.machine(), "kernel": platform.release()}
    if mode == "planned" and targets is not None:
        fingerprint["targets"] = targets
    return fingerprint


def container_command(source, output, image, command, *, network=None, postgres=False, name=None):
    args = ["docker", "create", "--name", name] if name else ["docker", "run", "--rm"]
    args += ["--init", "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp", "--shm-size=1g", "--mount", f"type=bind,src={source},dst=/app",
            "--mount", f"type=bind,src={output},dst=/evidence", "--workdir", "/app",
            "--env", "DJANGO_SETTINGS_MODULE=config.settings.test", "--env", "PYTHONUTF8=1"]
    if network:
        args += ["--network", network]
    if postgres:
        args += ["--env", "PHR_POSTGRES_TEST_URL=postgresql://phr_test:synthetic-local-only@postgres:5432/phr_test"]
    browser_probe = "import asyncio\nfrom playwright.async_api import async_playwright\nasync def main():\n async with async_playwright() as p:\n  print(p.chromium.executable_path)\nasyncio.run(main())\n"
    wrapper = "ln -s /opt/deps/node_modules node_modules; PHR_BROWSER_EXECUTABLE=$(python -c " + shlex.quote(browser_probe) + "); export PHR_BROWSER_EXECUTABLE; "
    return [*args, image, "bash", "-euc", wrapper + command]


def check_test_report(step, output, *, scoped=False):
    name = step["name"]
    if step["status"] == "passed" and (name in JUNIT_GROUPS or (scoped and name == "javascript")):
        try:
            report = output / (name + ".xml")
            step["tests"] = require_test_report(report, allow_skips=not scoped and name in {"python", "workflow"})
            if not scoped and name in {"python", "workflow"}:
                cases = list(ET.parse(report).getroot().iter("testcase"))
                skipped = [case for case in cases if case.find("skipped") is not None]
                step["skipped_tests"] = len(skipped)
                if name == "workflow":
                    allowed = ("tests.tools.test_submit_validation",
                               "test_windows_runner_uses_direct_wsl_arguments_for_paths_with_spaces",
                               "Windows WSL argument boundary")
                    if len(skipped) > 1 or any((case.get("classname"), case.get("name"),
                                              case.find("skipped").get("message")) != allowed for case in skipped):
                        raise ValueError("Workflow tests have an unapproved skip")
                    if len(skipped) == len(cases):
                        raise ValueError("Workflow tests contain no executed tests")
                    step["allowed_skips"] = [{"classname": case.get("classname"), "name": case.get("name"),
                                               "reason": case.find("skipped").get("message")} for case in skipped]
        except ValueError as error:
            step.update(status="failed", error=str(error))


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def documentation_input(name):
    # Keep this standalone runner aligned with submit_validation._documentation_path.
    evaluation = {"phase-two-baseline-manifest.json", "labs-extraction-scope-baseline-manifest.json",
                  "phase-two-annotation-adjudication-report.json"}
    if name.startswith("docs/deployment/local/") or name in {"docs/verification/artifacts/" + path for path in evaluation}:
        return False
    if name.startswith("prototype-gallery/screenshots/") and name.lower().endswith(STATIC_DESIGN_SUFFIXES):
        return True
    if name.startswith("docs/verification/artifacts/feature-pruning-browser/") and name.lower().endswith(".png"):
        return True
    if name in {"README.md", "AGENTS.md", "CHANGELOG.md", "docs/document-registry.json",
                "docs/verification/traceability.json", "docs/verification/release-evidence.json",
                ".github/pull_request_template.md"}:
        return True
    if name.endswith(".md") and name.startswith(("docs/", ".github/PULL_REQUEST_TEMPLATE/", ".github/ISSUE_TEMPLATE/")):
        return True
    parent, _, filename = name.rpartition("/")
    return ((parent == "docs/verification/artifacts" and filename.endswith((".json", ".xml")))
            or (parent == "docs/verification/attestations" and filename.endswith(".json")))


def group_input_digest(source, group):
    files = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise ValueError("Validation inputs must not contain symbolic links")
        if not path.is_file():
            continue
        name = relative.as_posix()
        executable = bool(path.stat().st_mode & 0o111)
        if group not in {"contracts", "release-tests", "production"} and not executable and documentation_input(name):
            continue
        files.append((name, executable, digest(path.read_bytes())))
    return digest(files)


def group_cache_key(source, group, fingerprint, targets=None):
    targets = fingerprint.get("targets") if targets is None else targets
    plan_fields = ({"required_steps", "command_digest", "targets"}
                   if group == "production" or "targets" in fingerprint else {"required_steps"})
    environment = {key: value for key, value in fingerprint.items() if key not in plan_fields}
    commands = command_catalog()
    command = ([commands["production-build"], commands["production-smoke"]] if group == "production"
               else scoped_test_command(group, targets[group]) if targets and group in TARGET_GROUPS else commands[group])
    inputs = group_input_digest(source, group)
    return digest({"schema": 1, "group": group, "inputs": inputs, "command": command,
                   "targets": targets[group] if targets and group in TARGET_GROUPS else [],
                   "environment": environment, "runner": digest(Path(__file__).read_bytes())})


def group_artifacts(group, steps, *, scoped=False):
    names = [step["name"] + ".log" for step in steps]
    if group in JUNIT_GROUPS or (scoped and group == "javascript"):
        names.append(group + ".xml")
    if group == "corpus":
        names.append("phase-two-release-evaluation.json")
    if group == "production":
        names.append("production.Dockerfile")
    return names


def save_group(cache, key, group, steps, output, metadata=None, *, scoped=False):
    if cache is None or any(step.get("status") != "passed" or step.get("returncode") != 0 for step in steps):
        return
    for step in steps:
        check_test_report(step, output, scoped=scoped)
    if any(step["status"] != "passed" or ((step["name"] in JUNIT_GROUPS or (scoped and step["name"] == "javascript"))
           and step.get("tests", 0) <= step.get("skipped_tests", 0)) for step in steps):
        return
    names = group_artifacts(group, steps, scoped=scoped)
    if any(not (output / name).is_file() or (output / name).is_symlink() for name in names):
        return
    entry = {"schema": 1, "key": key, "group": group, "steps": steps, "metadata": metadata or {},
             "artifacts": {name: digest((output / name).read_bytes()) for name in names}}
    entry["digest"] = digest(entry)
    destination = cache / "groups" / key
    destination.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(output / name, destination / name)
    temporary = destination / "receipt.tmp"
    temporary.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination / "receipt.json")


def restore_group(cache, key, group, output, *, scoped=False, targets=None):
    if cache is None:
        return None
    directory = cache / "groups" / key
    try:
        entry = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
        seal = entry.pop("digest")
        if (digest(entry) != seal or entry["schema"] != 1 or entry["key"] != key or entry["group"] != group
                or any(step.get("status") != "passed" or step.get("returncode") != 0 for step in entry["steps"])):
            return None
        expected = ["production-build", "production-smoke"] if group == "production" else [group]
        if [step["name"] for step in entry["steps"]] != expected:
            return None
        if scoped and any(step.get("targets") != targets for step in entry["steps"]):
            return None
        if set(entry["artifacts"]) != set(group_artifacts(group, entry["steps"], scoped=scoped)):
            return None
        for name, checksum in entry["artifacts"].items():
            if (directory / name).is_symlink() or digest((directory / name).read_bytes()) != checksum:
                return None
        if group == "production" and not all(entry["metadata"].get(name) for name in ("production_image", "production_recipe_sha256")):
            return None
        for step in entry["steps"]:
            check_test_report(step, directory, scoped=scoped)
            if (step["status"] != "passed" or ((step["name"] in JUNIT_GROUPS or (scoped and step["name"] == "javascript"))
                    and step.get("tests", 0) <= step.get("skipped_tests", 0))):
                return None
        for name in entry["artifacts"]:
            shutil.copyfile(directory / name, output / name)
        for step in entry["steps"]:
            step.update(reused=True, cache_key=key)
        return entry
    except (OSError, ValueError, TypeError, KeyError):
        return None


def parallel_python_postgres(source, output, fingerprint, workspace, network, database, steps,
                             names=("python", "postgres"), completed=None, commands=None, targets=None):
    records = {name: {"name": name, "status": "not-run", "seconds": 0} for name in names}
    steps.extend(records.values())
    executor = None
    futures = {}
    interrupted = False
    preparing = "postgres"
    try:
        if "postgres" in names:
            capture(["docker", "network", "create", "--internal", network])
            capture(["docker", "run", "--detach", "--name", database, "--network", network, "--network-alias", "postgres",
                     "--env", "POSTGRES_USER=phr_test", "--env", "POSTGRES_PASSWORD=synthetic-local-only", "--env", "POSTGRES_DB=phr_test",
                     "--tmpfs", "/var/lib/postgresql", fingerprint["base_images"]["postgres"]])
            for attempt in range(60):
                ready = subprocess.run(["docker", "exec", database, "pg_isready", "-U", "phr_test", "-d", "phr_test"], env=clean_environment(), capture_output=True)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Disposable PostgreSQL did not become ready")
        for preparing in names:
            step_source = workspace / preparing
            shutil.copytree(source, step_source)
            command = commands[preparing] if commands is not None else validation_commands("full")[preparing]
            capture(container_command(step_source, output, fingerprint["dependency_image"], command,
                                      network=network if preparing == "postgres" else None, postgres=preparing == "postgres",
                                      name=network + "-" + preparing))
        # Both containers exist before workers start, so cancellation cannot race creation.
        def execute_group(name):
            step = run_step(name, ["docker", "start", "--attach", network + "-" + name], output)
            scoped = targets is not None and name in targets and bool(targets[name])
            if scoped:
                step["targets"] = targets[name]
            check_test_report(step, output, scoped=scoped)
            if completed:
                completed(name, [step])
            return step

        executor = ThreadPoolExecutor(max_workers=2)
        for name in names:
            futures[name] = executor.submit(execute_group, name)
        wait(futures.values())
    except KeyboardInterrupt:
        interrupted = True
        for name, record in records.items():
            if name not in futures or not futures[name].done():
                record.update(status="failed", error="Validation interrupted")
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        log = output / (preparing + ".log")
        log.write_text(str(error) + "\n" + str(getattr(error, "stderr", "")), encoding="utf-8")
        records[preparing].update(status="failed", error=str(error), log=log.name)
    finally:
        # Stop only this run's test containers before joining, then let the caller remove DB/network.
        for name in names:
            subprocess.run(["docker", "rm", "--force", network + "-" + name], env=clean_environment(), capture_output=True)
        if executor:
            executor.shutdown(wait=True)
        for name, future in futures.items():
            try:
                records[name].update(future.result())
                check_test_report(records[name], output, scoped=targets is not None and bool(targets.get(name)))
            except Exception as error:
                records[name].update(status="failed", error=str(error))
            if interrupted and records[name]["status"] != "passed":
                records[name].update(status="failed", error="Validation interrupted")
    if interrupted:
        raise KeyboardInterrupt
    if any(record["status"] != "passed" for record in records.values()):
        raise RuntimeError("Python/PostgreSQL validation failed")


def execute_validation(source, output, fingerprint, mode, workspace, cache=None, groups=None, targets=None):
    steps = []
    selected = required_steps(mode, groups)
    if mode == "planned":
        targets = validate_targets(targets, selected, source)
        if fingerprint.get("targets") is not None and fingerprint["targets"] != targets:
            raise ValueError("Planned targets differ from environment fingerprint")
    network = "emr-validation-" + uuid.uuid4().hex
    database = network + "-db"
    production = network + ":production"
    result = {"schema": 1, "status": "failed", "mode": mode, "fingerprint": fingerprint, "steps": steps}
    if mode == "planned" and targets is not None:
        result["targets"] = targets
    try:
        shallow = source / ".git/shallow"
        revision = shallow.read_text(encoding="ascii").strip() if shallow.is_file() else None
        cache_groups = [name for name in selected if not name.startswith("production-")]
        if "production-build" in selected:
            cache_groups.append("production")
        keys = {name: group_cache_key(source, name, fingerprint, targets) for name in cache_groups} if cache and mode != "docs" else {}
        restored = {}
        for name, key in keys.items():
            scoped = targets is not None and name in TARGET_GROUPS and bool(targets[name])
            entry = restore_group(cache, key, name, output, scoped=scoped,
                                  targets=targets[name] if scoped else None)
            if entry:
                restored[name] = entry
                steps.extend(entry["steps"])
                result.update(entry["metadata"])

        def completed(name, records):
            if revision:
                for step in records:
                    step["validated_revision"] = revision
            scoped = targets is not None and name in TARGET_GROUPS and bool(targets[name])
            save_group(cache, keys.get(name), name, records, output, scoped=scoped)

        parallel_done = set()
        commands = validation_commands(mode, groups, targets) if mode == "planned" else validation_commands(mode)
        for name, command in commands.items():
            if name in restored or name in parallel_done:
                continue
            if name in {"python", "postgres"}:
                pending = tuple(group for group in ("python", "postgres") if group in commands and group not in restored)
                parallel_python_postgres(source, output, fingerprint, workspace, network, database, steps,
                                         names=pending, completed=completed, commands=commands, targets=targets)
                parallel_done.update(pending)
                continue
            if mode == "docs":
                step = run_step(name, [sys.executable, *command], output, cwd=source)
            else:
                step_source = workspace / name
                shutil.copytree(source, step_source)
                step = run_step(name, container_command(step_source, output, fingerprint["dependency_image"], command,
                                network=network if name == "postgres" else None, postgres=name == "postgres"), output)
            steps.append(step)
            scoped = targets is not None and name in TARGET_GROUPS and bool(targets[name])
            if scoped:
                step["targets"] = targets[name]
            check_test_report(step, output, scoped=scoped)
            if step["status"] != "passed":
                raise RuntimeError(name + " failed")
            if mode != "docs":
                completed(name, [step])
        if "production-build" not in selected or "production" in restored:
            result["status"] = "passed"
            return result
        recipe = pinned_production_recipe(source / "deploy/Dockerfile", fingerprint["base_images"]["python"])
        pinned = output / "production.Dockerfile"
        pinned.write_text(recipe, encoding="utf-8")
        result["production_recipe_sha256"] = hashlib.sha256(recipe.encode()).hexdigest()
        build = run_step("production-build", ["docker", "build", "--file", str(pinned), "--tag", production, "."], output, cwd=source)
        steps.append(build)
        if build["status"] != "passed":
            raise RuntimeError("Production image build failed")
        steps.append(run_step("production-smoke", ["docker", "run", "--rm", "--env", "DJANGO_SETTINGS_MODULE=config.settings.build", "--entrypoint", "python", production, "-c", PRODUCTION_SMOKE], output))
        if all(step["status"] == "passed" for step in steps) and {step["name"] for step in steps} == set(selected):
            result["production_image"] = image_id(production)
            if revision:
                for step in steps[-2:]:
                    step["validated_revision"] = revision
            save_group(cache, keys.get("production"), "production", steps[-2:], output,
                       {name: result[name] for name in ("production_image", "production_recipe_sha256")})
            result["status"] = "passed"
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        result["error"] = str(error) or "Validation interrupted"
    finally:
        if mode != "docs":
            for command in (["docker", "rm", "--force", database], ["docker", "network", "rm", network]):
                subprocess.run(command, env=clean_environment(), capture_output=True)
        completed = {step["name"]: step for step in steps}
        steps[:] = [completed.get(name, {"name": name, "status": "not-run", "seconds": 0}) for name in selected]
        (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--git-pack", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--mode", choices=("full", "release", "docs", "planned"), default="full")
    parser.add_argument("--groups", help="JSON array of selected validation groups for planned mode")
    parser.add_argument("--targets", help="JSON object of exact test paths for planned mode")
    parser.add_argument("--fingerprint", action="store_true")
    args = parser.parse_args(arguments)
    try:
        groups = json.loads(args.groups) if args.groups is not None else None
        targets = json.loads(args.targets) if args.targets is not None else None
        if (groups is not None or targets is not None) and args.mode != "planned":
            raise ValueError("--groups and --targets require planned mode")
        selected = required_steps(args.mode, groups)
        if args.mode == "planned":
            targets = validate_targets(targets, selected)
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    if sys.platform != "linux":
        parser.error("Run this tool inside WSL/Linux")
    if not args.fingerprint and not (args.output and args.git_pack and args.revision):
        parser.error("--output, --git-pack and --revision are required for validation")
    cache = Path.home() / ".cache/emr-submit"
    cache.mkdir(parents=True, exist_ok=True)
    # Cross-process lock keeps dependency builds and all tests below two heavy jobs.
    import fcntl
    with (cache / "validation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix="run-", dir=cache) as directory:
            workspace = Path(directory)
            source = workspace / "source"
            try:
                extract_archive(args.archive, source)
                if not args.fingerprint:
                    initialize_source_git(source, args.git_pack, args.revision)
                fingerprint = (prepare_environment(source, cache, args.mode, groups, targets) if args.mode == "planned"
                               else prepare_environment(source, cache, args.mode))
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
                result = {"schema": 1, "status": "failed", "mode": args.mode, "error": str(error),
                          "steps": [{"name": name, "status": "not-run", "seconds": 0} for name in required_steps(args.mode, groups)]}
                if args.output:
                    args.output.mkdir(parents=True, exist_ok=True)
                    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                print(str(error), file=sys.stderr)
                return 1
            if args.fingerprint:
                print(json.dumps(fingerprint, sort_keys=True))
                return 0
            output = workspace / "evidence"
            output.mkdir()
            result = execute_validation(source, output, fingerprint, args.mode, workspace, cache=cache,
                                        groups=groups, targets=targets)
            args.output.mkdir(parents=True, exist_ok=True)
            shutil.copytree(output, args.output, dirs_exist_ok=True)
            print(json.dumps({"status": result["status"], "output": str(args.output)}))
            return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
