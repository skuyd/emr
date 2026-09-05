from pathlib import Path
import subprocess

import pytest

from tools import update_dependency_locks


@pytest.mark.parametrize("upgrade", [False, True])
def test_lock_generation_preserves_both_existing_pins_unless_upgrade_requested(tmp_path, monkeypatch, upgrade):
    (tmp_path / "pyproject.toml").write_text(
        '[project.optional-dependencies]\ntest = ["pytest>=8,<9"]\n', encoding="utf-8",
    )
    production = tmp_path / "requirements-prod.lock"
    testing = tmp_path / "requirements-test.lock"
    production.write_text("django==5.2.17\n", encoding="utf-8")
    testing.write_text("pytest==8.4.2\n", encoding="utf-8")
    calls = []

    def compile_lock(command, *, cwd, check):
        calls.append(command)
        assert cwd == tmp_path and check is True
        output = Path(command[command.index("--output-file") + 1])
        output.write_text("synthetic==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(update_dependency_locks, "ROOT", tmp_path)
    monkeypatch.setattr(update_dependency_locks.subprocess, "run", compile_lock)
    monkeypatch.setattr("sys.argv", ["update_dependency_locks", *(["--upgrade"] if upgrade else [])])

    assert update_dependency_locks.main() == 0

    def constraints(command):
        return {Path(command[index + 1]) for index, value in enumerate(command) if value == "--constraint"}

    generated_production = Path(calls[0][calls[0].index("--output-file") + 1])
    assert constraints(calls[0]) == (set() if upgrade else {production})
    assert constraints(calls[1]) == ({generated_production} if upgrade else {generated_production, testing})
    for command in calls:
        assert "--generate-hashes" in command
        assert command[command.index("--python-platform") + 1] == "x86_64-unknown-linux-gnu"
        assert command[command.index("--python-version") + 1] == "3.11"
