import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "start-local.ps1"


pytestmark = pytest.mark.skipif(os.name != "nt", reason="The local launcher targets Windows PowerShell")


def _powershell():
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


def _local_fixture(tmp_path, directory_name="local-launcher"):
    assert SCRIPT.is_file(), "deploy/start-local.ps1 must exist"
    fixture_root = tmp_path / directory_name
    deploy_dir = fixture_root / "deploy"
    deploy_dir.mkdir(parents=True)
    shutil.copy2(SCRIPT, deploy_dir / SCRIPT.name)
    (fixture_root / ".env").write_text("DJANGO_DEBUG=True\nOTP_PROVIDER=development\n", encoding="utf-8")
    (fixture_root / "manage.py").write_text(
        """import json
import os
from pathlib import Path
import sys
import time

record = {
    "arguments": sys.argv[1:],
    "settings": os.environ.get("DJANGO_SETTINGS_MODULE"),
    "pid": os.getpid(),
}
log_path = Path(os.environ["START_LOCAL_TEST_LOG"])
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
    stream.flush()
if sys.argv[1:2] == ["run_local_processing_worker"]:
    time.sleep(60)
""",
        encoding="utf-8",
    )
    return fixture_root


def _run_launcher(fixture_root, *arguments, timeout=15):
    log_path = fixture_root / "management-commands.jsonl"
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
    environment["START_LOCAL_TEST_LOG"] = str(log_path)
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(fixture_root / "deploy" / SCRIPT.name),
            "-PythonPath",
            sys.executable,
            *arguments,
        ],
        cwd=fixture_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    records = []
    if log_path.exists():
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    return completed, records


def test_launcher_prepares_dev_environment_and_runs_web_in_foreground(tmp_path):
    fixture_root = _local_fixture(tmp_path)

    completed, records = _run_launcher(
        fixture_root,
        "-NoWorker",
        "-NoSeed",
        "-Address",
        "127.0.0.1:8123",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert [record["arguments"] for record in records] == [
        ["migrate", "--noinput"],
        ["check"],
        ["runserver", "127.0.0.1:8123"],
    ]
    assert {record["settings"] for record in records} == {"config.settings.dev"}
    assert "http://127.0.0.1:8123" in completed.stdout


@pytest.mark.parametrize("directory_name", ["local-launcher", "local launcher"])
def test_launcher_runs_seed_and_worker_then_stops_worker_with_web(tmp_path, directory_name):
    fixture_root = _local_fixture(tmp_path, directory_name)
    worker_pid = None

    try:
        completed, records = _run_launcher(fixture_root, "-Address", "localhost:8124")
        assert completed.returncode == 0, completed.stdout + completed.stderr
        worker_record = next(
            record for record in records if record["arguments"] == ["run_local_processing_worker"]
        )
        worker_pid = worker_record["pid"]

        assert [record["arguments"] for record in records[:3]] == [
            ["migrate", "--noinput"],
            ["check"],
            ["seed_development_account"],
        ]
        assert ["runserver", "localhost:8124"] in [record["arguments"] for record in records]
        assert "Local processing worker started" in completed.stdout
        assert "Local processing worker stopped" in completed.stdout
        log_dir = fixture_root / ".runtime" / "logs"
        assert tuple(log_dir.glob("local-processing-worker-*.stdout.log"))
        assert tuple(log_dir.glob("local-processing-worker-*.stderr.log"))
    finally:
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGTERM)
            except OSError:
                pass


def test_launcher_rejects_non_loopback_bind_address_before_running_commands(tmp_path):
    fixture_root = _local_fixture(tmp_path)

    completed, records = _run_launcher(
        fixture_root,
        "-NoWorker",
        "-NoSeed",
        "-Address",
        "0.0.0.0:8000",
    )

    assert completed.returncode != 0
    assert records == []
    assert "loopback address" in (completed.stdout + completed.stderr)
