"""Run an exact Git archive in disposable Linux containers, without GitHub secrets."""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
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


def validation_commands(mode):
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


def required_steps(mode):
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
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Archive contains an unsafe path or link")
        stream.extractall(destination, members=members, filter="data")


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


def prepare_environment(source, cache):
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
    policy = {mode: validation_commands(mode) for mode in ("full", "release")}
    command_digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode() + Path(__file__).read_bytes()).hexdigest()
    return {"schema": 1, "command_digest": command_digest,
            "required_steps": {mode: required_steps(mode) for mode in ("full", "release")},
            "dependency_key": key, "dependency_image": dependency_id, "base_images": base_ids,
            "docker": json.loads(capture(["docker", "version", "--format", "{{json .Server}}"]))["Version"],
            "architecture": platform.machine(), "kernel": platform.release()}


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


def check_test_report(step, output):
    name = step["name"]
    if step["status"] == "passed" and name in {"python", "browser", "postgres", "release-tests"}:
        try:
            step["tests"] = require_test_report(output / (name + ".xml"), allow_skips=name == "python")
            if name == "python":
                step["skipped_tests"] = len(list(ET.parse(output / "python.xml").getroot().iter("skipped")))
        except ValueError as error:
            step.update(status="failed", error=str(error))


def parallel_python_postgres(source, output, fingerprint, workspace, network, database, steps):
    names = ("python", "postgres")
    records = {name: {"name": name, "status": "not-run", "seconds": 0} for name in names}
    steps.extend(records.values())
    executor = None
    futures = {}
    interrupted = False
    preparing = "postgres"
    try:
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
            capture(container_command(step_source, output, fingerprint["dependency_image"], validation_commands("full")[preparing],
                                      network=network if preparing == "postgres" else None, postgres=preparing == "postgres",
                                      name=network + "-" + preparing))
        # Both containers exist before workers start, so cancellation cannot race creation.
        executor = ThreadPoolExecutor(max_workers=2)
        for name in names:
            futures[name] = executor.submit(run_step, name, ["docker", "start", "--attach", network + "-" + name], output)
        wait(futures.values())
    except KeyboardInterrupt:
        interrupted = True
        for record in records.values():
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
                check_test_report(records[name], output)
            except Exception as error:
                records[name].update(status="failed", error=str(error))
            if interrupted:
                records[name].update(status="failed", error="Validation interrupted")
    if any(record["status"] != "passed" for record in records.values()):
        raise RuntimeError("Python/PostgreSQL validation failed")


def execute_validation(source, output, fingerprint, mode, workspace):
    steps = []
    network = "emr-validation-" + uuid.uuid4().hex
    database = network + "-db"
    production = network + ":production"
    result = {"schema": 1, "status": "failed", "mode": mode, "fingerprint": fingerprint, "steps": steps}
    try:
        for name, command in validation_commands(mode).items():
            if mode == "full" and name == "python":
                parallel_python_postgres(source, output, fingerprint, workspace, network, database, steps)
                continue
            if mode == "full" and name == "postgres":
                continue
            step_source = workspace / name
            shutil.copytree(source, step_source)
            step = run_step(name, container_command(step_source, output, fingerprint["dependency_image"], command,
                            network=network if name == "postgres" else None, postgres=name == "postgres"), output)
            steps.append(step)
            check_test_report(step, output)
            if step["status"] != "passed":
                raise RuntimeError(name + " failed")
        recipe = pinned_production_recipe(source / "deploy/Dockerfile", fingerprint["base_images"]["python"])
        pinned = output / "production.Dockerfile"
        pinned.write_text(recipe, encoding="utf-8")
        result["production_recipe_sha256"] = hashlib.sha256(recipe.encode()).hexdigest()
        build = run_step("production-build", ["docker", "build", "--file", str(pinned), "--tag", production, "."], output, cwd=source)
        steps.append(build)
        if build["status"] != "passed":
            raise RuntimeError("Production image build failed")
        smoke = "import os; assert os.getuid() == 10001; import django, celery, pypdfium2; from apps.exports.files import private_temporary_file; output=private_temporary_file(); output.write(b'synthetic-export-probe'); output.close(); print('Production image imports and private export temp write verified')"
        steps.append(run_step("production-smoke", ["docker", "run", "--rm", "--env", "DJANGO_SETTINGS_MODULE=config.settings.build", "--entrypoint", "python", production, "-c", smoke], output))
        if all(step["status"] == "passed" for step in steps) and {step["name"] for step in steps} == set(required_steps(mode)):
            result["production_image"] = image_id(production)
            result["status"] = "passed"
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        result["error"] = str(error) or "Validation interrupted"
    finally:
        for command in (["docker", "rm", "--force", database], ["docker", "network", "rm", network]):
            subprocess.run(command, env=clean_environment(), capture_output=True)
        completed = {step["name"]: step for step in steps}
        steps[:] = [completed.get(name, {"name": name, "status": "not-run", "seconds": 0}) for name in required_steps(mode)]
        (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("full", "release"), default="full")
    parser.add_argument("--fingerprint", action="store_true")
    args = parser.parse_args(arguments)
    if sys.platform != "linux":
        parser.error("Run this tool inside WSL/Linux")
    if not args.fingerprint and not args.output:
        parser.error("--output is required for validation")
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
                fingerprint = prepare_environment(source, cache)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
                result = {"schema": 1, "status": "failed", "mode": args.mode, "error": str(error),
                          "steps": [{"name": name, "status": "not-run", "seconds": 0} for name in required_steps(args.mode)]}
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
            result = execute_validation(source, output, fingerprint, args.mode, workspace)
            args.output.mkdir(parents=True, exist_ok=True)
            shutil.copytree(output, args.output, dirs_exist_ok=True)
            print(json.dumps({"status": result["status"], "output": str(args.output)}))
            return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
