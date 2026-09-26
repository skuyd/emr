import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from tools import local_validation as validation


def test_dependency_key_ignores_release_version_but_tracks_dependency_and_recipe(tmp_path):
    (tmp_path / 'deploy').mkdir()
    (tmp_path / 'deploy/local-validation.Dockerfile').write_text('FROM python:3.11\n')
    for name in ('requirements-prod.lock', 'requirements-test.lock'):
        (tmp_path / name).write_text('locked==1 --hash=sha256:abc\n')
    (tmp_path / 'package.json').write_text(json.dumps({'version': '1.0.0', 'devDependencies': {'tool': '1'}}))
    lock = {'version': '1.0.0', 'packages': {'': {'version': '1.0.0'}, 'node_modules/tool': {'version': '1'}}}
    (tmp_path / 'package-lock.json').write_text(json.dumps(lock))
    original = validation.dependency_key(tmp_path, {'python': 'sha256:one'})
    package = json.loads((tmp_path / 'package.json').read_text())
    package['version'] = '1.1.0'
    (tmp_path / 'package.json').write_text(json.dumps(package))
    lock['version'] = lock['packages']['']['version'] = '1.1.0'
    (tmp_path / 'package-lock.json').write_text(json.dumps(lock))
    assert validation.dependency_key(tmp_path, {'python': 'sha256:one'}) == original
    assert validation.dependency_key(tmp_path, {'python': 'sha256:two'}) != original
    lock['packages']['node_modules/tool']['version'] = '2'
    (tmp_path / 'package-lock.json').write_text(json.dumps(lock))
    assert validation.dependency_key(tmp_path, {'python': 'sha256:one'}) != original
    (tmp_path / 'deploy/local-validation.Dockerfile').write_text('FROM python:3.12\n')
    assert validation.dependency_key(tmp_path, {'python': 'sha256:one'}) != original


@pytest.mark.parametrize('body', ['<testsuites/>', '<testsuites><testcase><skipped/></testcase></testsuites>', '<testsuites><testcase><failure/></testcase></testsuites>', 'broken'])
def test_junit_rejects_empty_skipped_failed_or_invalid_reports(tmp_path, body):
    report = tmp_path / 'tests.xml'
    report.write_text(body)
    with pytest.raises(ValueError):
        validation.require_test_report(report)


def test_junit_accepts_nonempty_passed_tests(tmp_path):
    report = tmp_path / 'tests.xml'
    report.write_text('<testsuites><testsuite><testcase/><testcase/></testsuite></testsuites>')
    assert validation.require_test_report(report) == 2


def test_failed_command_records_log_and_failure_without_claiming_success(tmp_path):
    step = validation.run_step('example', [sys.executable, '-c', 'print("failure evidence"); raise SystemExit(7)'], tmp_path)
    assert step['status'] == 'failed'
    assert step['returncode'] == 7
    assert step['seconds'] >= 0
    assert 'failure evidence' in (tmp_path / step['log']).read_text()


def test_command_uses_clean_environment_without_github_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'do-not-pass')
    monkeypatch.setenv('GITHUB_TOKEN', 'do-not-pass-either')
    step = validation.run_step('environment', [sys.executable, '-c', 'import os; assert "GH_TOKEN" not in os.environ; assert "GITHUB_TOKEN" not in os.environ'], tmp_path)
    assert step['status'] == 'passed'


@pytest.mark.parametrize('name,kind', [('../escape', 'file'), ('/absolute', 'file'), ('link', 'symlink')])
def test_extract_rejects_paths_outside_run_and_links(tmp_path, name, kind):
    archive = tmp_path / 'source.tar'
    with tarfile.open(archive, 'w') as stream:
        member = tarfile.TarInfo(name)
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = '/etc/passwd'
            stream.addfile(member)
        else:
            member.size = 1
            stream.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(ValueError):
        validation.extract_archive(archive, tmp_path / 'source')


def test_full_policy_preserves_required_ci_scope():
    steps = validation.validation_commands('full')
    assert set(steps) == {'contracts', 'django', 'python', 'browser', 'javascript', 'postgres', 'corpus'}
    assert 'not postgres and not ocr_model' in steps['python']
    assert steps['python'].count('--ignore=tests/browser/') == 8
    assert steps['browser'].count('tests/browser/') == 8
    assert 'run_required_tests.py' in steps['browser']
    assert '--ds=config.settings.postgres_test -m postgres tests' in steps['postgres']
    assert 'phase_two_evaluation.py --synthetic-only' in steps['corpus']
    assert {'production-build', 'production-smoke'} <= set(validation.required_steps('release'))


def test_execution_failure_persists_receipt_and_marks_remaining_steps_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(validation.os, 'getuid', lambda: 1000, raising=False)
    monkeypatch.setattr(validation.os, 'getgid', lambda: 1000, raising=False)
    source = tmp_path / 'source'
    source.mkdir()
    output = tmp_path / 'output'
    output.mkdir()
    def failed_step(name, command, destination, **kwargs):
        return {'name': name, 'status': 'failed', 'returncode': 9, 'seconds': 0, 'log': name + '.log'}
    monkeypatch.setattr(validation, 'run_step', failed_step)
    monkeypatch.setattr(validation.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    result = validation.execute_validation(source, output, {'dependency_image': 'sha256:test'}, 'full', tmp_path)
    assert result['status'] == 'failed'
    assert result['steps'][0]['status'] == 'failed'
    assert all(step['status'] == 'not-run' for step in result['steps'][1:])
    assert json.loads((output / 'result.json').read_text()) == result


def test_general_python_report_allows_existing_platform_skips(tmp_path):
    report = tmp_path / 'tests.xml'
    report.write_text('<testsuites><testcase/><testcase><skipped message="Windows only"/></testcase></testsuites>')
    assert validation.require_test_report(report, allow_skips=True) == 2


def test_container_runs_as_owner_so_generated_files_can_be_cleaned(tmp_path, monkeypatch):
    monkeypatch.setattr(validation.os, 'getuid', lambda: 1234, raising=False)
    monkeypatch.setattr(validation.os, 'getgid', lambda: 1234, raising=False)
    command = validation.container_command(tmp_path, tmp_path, 'sha256:example', 'python manage.py check')
    assert command[command.index('--user') + 1] == '1234:1234'
    assert 'HOME=/tmp' in command


def test_base_reference_uses_registry_digest_accepted_by_dockerfile_from(monkeypatch):
    monkeypatch.setattr(validation, 'capture', lambda command: json.dumps([{'Id': 'sha256:local', 'RepoDigests': ['python@sha256:registry']}]))
    assert validation.base_reference('python:3.11') == 'python@sha256:registry'


def test_production_recipe_pins_only_the_declared_base_and_rejects_drift(tmp_path):
    recipe = tmp_path / 'Dockerfile'
    recipe.write_text('FROM python:3.11.16-slim-bookworm\nRUN echo keep-this\n')
    assert validation.pinned_production_recipe(recipe, 'python@sha256:fixed') == 'FROM python@sha256:fixed\nRUN echo keep-this\n'
    recipe.write_text('FROM python:3.12\nRUN echo keep-this\n')
    with pytest.raises(ValueError, match='base'):
        validation.pinned_production_recipe(recipe, 'python@sha256:fixed')


def test_bootstrap_failure_replaces_stale_passed_result(tmp_path, monkeypatch):
    from types import SimpleNamespace
    archive = tmp_path / 'empty.tar'
    with tarfile.open(archive, 'w'):
        pass
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'result.json').write_text('{"status":"passed"}')
    monkeypatch.setattr(validation.sys, 'platform', 'linux')
    monkeypatch.setattr(validation.Path, 'home', lambda: tmp_path)
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(LOCK_EX=1, flock=lambda *args: None))
    def unavailable(*args):
        raise RuntimeError('Docker bootstrap failed')
    monkeypatch.setattr(validation, 'prepare_environment', unavailable)
    assert validation.main(['--archive', str(archive), '--output', str(output)]) == 1
    result = json.loads((output / 'result.json').read_text())
    assert result['status'] == 'failed'
    assert result['error'] == 'Docker bootstrap failed'


def test_missing_production_image_identity_cannot_leave_passed_status(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    (source / 'deploy').mkdir(parents=True)
    (source / 'deploy/Dockerfile').write_text('FROM python:3.11.16-slim-bookworm\n')
    output = tmp_path / 'output'
    output.mkdir()
    monkeypatch.setattr(validation, 'validation_commands', lambda mode: {})
    monkeypatch.setattr(validation, 'run_step', lambda name, *args, **kwargs: {'name': name, 'status': 'passed', 'seconds': 0, 'returncode': 0})
    monkeypatch.setattr(validation.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    def missing_image(reference):
        raise subprocess.CalledProcessError(1, ['docker', 'inspect', reference])
    monkeypatch.setattr(validation, 'image_id', missing_image)
    result = validation.execute_validation(source, output, {'base_images': {'python': 'python@sha256:fixed'}}, 'full', tmp_path)
    assert result['status'] == 'failed'


def test_missing_python_report_marks_the_step_failed(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    output = tmp_path / 'output'
    output.mkdir()
    monkeypatch.setattr(validation.os, 'getuid', lambda: 1000, raising=False)
    monkeypatch.setattr(validation.os, 'getgid', lambda: 1000, raising=False)
    monkeypatch.setattr(validation, 'run_step', lambda name, *args, **kwargs: {'name': name, 'status': 'passed', 'seconds': 0, 'returncode': 0})
    monkeypatch.setattr(validation.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    result = validation.execute_validation(source, output, {'dependency_image': 'sha256:test'}, 'full', tmp_path)
    assert result['status'] == 'failed'
    assert next(step for step in result['steps'] if step['name'] == 'python')['status'] == 'failed'


def test_dependency_build_exports_uncompressed_local_image(monkeypatch, tmp_path):
    built = []
    monkeypatch.setattr(validation, 'base_reference', lambda reference: reference + '@sha256:fixed')
    monkeypatch.setattr(validation, 'capture', lambda command: '{"Version":"test-docker"}')
    def inspect(reference):
        if not built:
            raise subprocess.CalledProcessError(1, ['docker', 'inspect', reference])
        return 'sha256:built-image'
    def build(name, command, output, **kwargs):
        built.append(command)
        return {'name': name, 'status': 'passed', 'returncode': 0, 'seconds': 0}
    monkeypatch.setattr(validation, 'image_id', inspect)
    monkeypatch.setattr(validation, 'run_step', build)
    fingerprint = validation.prepare_environment(Path(__file__).resolve().parents[2], tmp_path)
    assert fingerprint['dependency_image'] == 'sha256:built-image'
    assert built[0][built[0].index('--output') + 1] == 'type=image,compression=uncompressed'


def test_dependency_key_normalizes_windows_checkout_line_endings(tmp_path):
    source = Path(__file__).resolve().parents[2]
    for name in validation.DEPENDENCY_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    before = validation.dependency_key(tmp_path, {'python': 'digest'})
    for name in validation.DEPENDENCY_FILES:
        target = tmp_path / name
        target.write_bytes(target.read_bytes().replace(b'\r\n', b'\n'))
    assert validation.dependency_key(tmp_path, {'python': 'digest'}) == before
