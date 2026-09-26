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


@pytest.mark.parametrize('option', ['--junitxml', '--junitxml='])
def test_required_test_runner_preserves_explicit_report(monkeypatch, tmp_path, option):
    report = tmp_path / 'persistent.xml'
    arguments = [option, str(report)] if option == '--junitxml' else [option + str(report)]
    def run(command, **kwargs):
        reports = []
        for index, value in enumerate(command):
            if value == '--junitxml':
                reports.append(command[index + 1])
            elif value.startswith('--junitxml='):
                reports.append(value.split('=', 1)[1])
        from pathlib import Path
        Path(reports[-1]).write_text('<testsuites><testcase/></testsuites>', encoding='utf-8')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', run)
    assert main(arguments) == 0
    assert report.is_file()


def test_required_test_runner_does_not_accept_a_stale_explicit_report(monkeypatch, tmp_path):
    report = tmp_path / 'stale.xml'
    report.write_text('<testsuites><testcase/></testsuites>')
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    assert main(['--junitxml', str(report)]) == 1
