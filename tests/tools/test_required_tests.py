import subprocess

import pytest

from tools.run_required_tests import main


@pytest.mark.parametrize("report, expected", [
    ('<testsuites><testsuite tests="1"><testcase/></testsuite></testsuites>', 0),
    ('<testsuites><testsuite tests="1"><testcase><skipped/></testcase></testsuite></testsuites>', 1),
    ('<testsuites><testsuite tests="0"/></testsuites>', 1),
])
def test_required_test_runner_rejects_skips_and_empty_selection(monkeypatch, report, expected):
    def run(command, **kwargs):
        from pathlib import Path
        path = command[command.index("--junitxml") + 1]
        Path(path).write_text(report, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert main(["-m", "postgres"]) == expected
