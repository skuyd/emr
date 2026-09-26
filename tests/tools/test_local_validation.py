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


@pytest.mark.parametrize('name,kind', [('../escape', 'file'), ('/absolute', 'file'), ('link', 'symlink'), ('.git/config', 'file'), ('nested/.git/config', 'file')])
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


@pytest.fixture
def git_snapshot(tmp_path):
    from tools import submit_validation as engine
    repo = tmp_path / 'original'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args]).decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Synthetic')
    git('config', 'user.email', 'synthetic@example.invalid')
    git('config', 'core.autocrlf', 'false')
    private = repo / 'deleted-private.txt'
    private.write_bytes(b'synthetic private historical content')
    git('add', '.')
    git('commit', '-qm', 'private historical fixture')
    old_commit = git('rev-parse', 'HEAD')
    private_blob = git('rev-parse', 'HEAD:deleted-private.txt')
    private.unlink()
    (repo / '.gitattributes').write_bytes(b'*.md text eol=lf\n')
    (repo / 'document.md').write_bytes(b'exact LF source\n')
    git('add', '-A')
    git('commit', '-qm', 'public candidate')
    revision = git('rev-parse', 'HEAD')
    git('remote', 'add', 'origin', 'https://synthetic:credential@example.invalid/private')
    git('config', 'credential.helper', 'synthetic-sensitive-helper')
    (repo / '.git/hooks/private-hook').write_text('synthetic hook')
    archive, pack = tmp_path / 'source.tar', tmp_path / 'source.pack'
    git('-c', 'core.autocrlf=false', 'archive', '--format=tar', f'--output={archive}', revision)
    engine._export_git_pack(repo, revision, pack)
    source = tmp_path / 'source'
    validation.extract_archive(archive, source)
    return repo, source, pack, revision, old_commit, private_blob


def test_snapshot_git_is_exact_shallow_candidate_without_host_history_or_config(git_snapshot):
    repo, source, pack, revision, old_commit, private_blob = git_snapshot
    validation.initialize_source_git(source, pack, revision)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args]).decode().strip()
    assert git('rev-parse', 'HEAD') == revision
    assert git('rev-parse', 'HEAD^{tree}') == subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}']).decode().strip()
    assert git('ls-files').splitlines() == ['.gitattributes', 'document.md']
    assert git('check-attr', 'eol', '--', 'document.md') == 'document.md: eol: lf'
    assert git('status', '--porcelain') == ''
    assert git('rev-list', 'HEAD') == revision
    assert git('remote') == ''
    config = (source / '.git/config').read_text()
    assert 'credential' not in config and 'example.invalid' not in config
    assert not (source / '.git/hooks').exists()
    for missing in (old_commit, private_blob):
        assert subprocess.run(['git', '-C', str(source), 'cat-file', '-e', missing], capture_output=True).returncode != 0


@pytest.mark.parametrize('damage', ['pack', 'revision', 'extra-object', 'source-bytes', 'source-extra', 'empty-source'])
def test_snapshot_git_rejects_invalid_or_mismatched_metadata(git_snapshot, damage):
    repo, source, pack, revision, old_commit, private_blob = git_snapshot
    if damage == 'pack':
        pack.write_bytes(b'not a pack')
    elif damage == 'revision':
        revision = old_commit
    elif damage == 'extra-object':
        objects = subprocess.check_output(['git', '-C', str(repo), 'rev-list', '--objects', '--all'])
        ids = b'\n'.join(line.split(b' ')[0] for line in objects.splitlines()) + b'\n'
        pack.write_bytes(subprocess.check_output(['git', '-C', str(repo), 'pack-objects', '--stdout', '--window=0'], input=ids))
    elif damage == 'source-bytes':
        (source / 'document.md').write_bytes(b'changed')
    elif damage == 'source-extra':
        (source / 'untracked').write_bytes(b'extra')
    else:
        for path in source.iterdir():
            path.unlink()
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        validation.initialize_source_git(source, pack, revision)
    assert not (source / '.git').exists()


@pytest.mark.parametrize('revision', ['../outside', '--help', 'HEAD'])
def test_snapshot_git_rejects_revision_path_escape_before_creating_metadata(tmp_path, revision):
    source = tmp_path / 'source'
    source.mkdir()
    with pytest.raises(ValueError):
        validation.initialize_source_git(source, tmp_path / 'missing.pack', revision)
    assert not (source / '.git').exists()


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
    monkeypatch.setattr(validation, 'initialize_source_git', lambda *args: None)
    assert validation.main(['--archive', str(archive), '--output', str(output), '--git-pack', 'fixture.pack', '--revision', 'a' * 40]) == 1
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
    monkeypatch.setattr(validation, 'capture', lambda command: 'container')
    result = validation.execute_validation(source, output, {'dependency_image': 'sha256:test', 'base_images': {'postgres': 'postgres@sha256:fixed'}}, 'full', tmp_path)
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


@pytest.fixture
def parallel_harness(tmp_path, monkeypatch):
    import concurrent.futures
    import threading
    from types import SimpleNamespace

    source = tmp_path / 'source'
    (source / 'deploy').mkdir(parents=True)
    (source / 'deploy/Dockerfile').write_text('FROM python:3.11.16-slim-bookworm\n')
    (source / 'original.txt').write_text('exact candidate')
    output = tmp_path / 'output'
    output.mkdir()
    state = SimpleNamespace(events=[], created=[], active=0, maximum=0, fail=None, skip=None,
                            setup_fail=False, create_fail=None, worker_error=None,
                            both_started=threading.Event(), barrier=threading.Barrier(2),
                            postgres_done=threading.Event(), cancelled=False,
                            stopped={name: threading.Event() for name in ('python', 'postgres')})
    guard = threading.Lock()
    monkeypatch.setattr(validation.os, 'getuid', lambda: 1000, raising=False)
    monkeypatch.setattr(validation.os, 'getgid', lambda: 1000, raising=False)
    monkeypatch.setattr(validation.uuid, 'uuid4', lambda: SimpleNamespace(hex='parallel-test'))
    monkeypatch.setattr(validation, 'image_id', lambda reference: 'sha256:production')

    def capture(command):
        if command[:3] == ['docker', 'network', 'create'] and state.setup_fail:
            raise subprocess.CalledProcessError(1, command, stderr='synthetic network failure')
        if command[:2] == ['docker', 'create']:
            if state.create_fail and command[command.index('--name') + 1].endswith('-' + state.create_fail):
                raise subprocess.CalledProcessError(1, command, stderr='synthetic create failure')
            state.created.append(command)
        return 'synthetic-container'

    def docker_run(command, **kwargs):
        if command[:3] == ['docker', 'rm', '--force']:
            state.events.append(('remove', command[-1]))
            for name in ('python', 'postgres'):
                if command[-1] == 'emr-validation-parallel-test-' + name:
                    state.stopped[name].set()
        elif command[:3] == ['docker', 'network', 'rm']:
            state.events.append(('network-remove', command[-1]))
        return subprocess.CompletedProcess(command, 0)

    def run_step(name, command, destination, **kwargs):
        if name in ('python', 'postgres'):
            assert len(state.created) == 2, 'both test containers must exist before either starts'
            assert command == ['docker', 'start', '--attach', 'emr-validation-parallel-test-' + name]
            assert (tmp_path / name / 'original.txt').read_text() == 'exact candidate'
            (tmp_path / name / 'owned.txt').write_text(name)
            with guard:
                state.active += 1
                state.maximum = max(state.maximum, state.active)
                state.events.append(('start', name))
                if state.active == 2:
                    state.both_started.set()
            state.barrier.wait(timeout=3)
            if state.cancelled:
                assert state.stopped[name].wait(timeout=3), 'cancel must stop container before joining worker'
            elif name == 'python':
                assert state.postgres_done.wait(timeout=3)
            with guard:
                state.active -= 1
                state.events.append(('end', name))
            if name == 'postgres':
                state.postgres_done.set()
            if name == state.worker_error:
                raise OSError('synthetic worker failure')
        else:
            assert state.active == 0, 'later groups must wait for both parallel groups'
            state.events.append(('start', name))
            state.events.append(('end', name))
        log = destination / (name + '.log')
        log.write_text('executed ' + name)
        if name in ('python', 'postgres', 'browser', 'release-tests'):
            child = '<skipped/>' if state.skip == name else ''
            (destination / (name + '.xml')).write_text('<testsuites><testcase>' + child + '</testcase></testsuites>')
        code = 137 if state.cancelled and name in ('python', 'postgres') else (7 if state.fail == name else 0)
        return {'name': name, 'status': 'passed' if code == 0 else 'failed', 'returncode': code,
                'seconds': 0.01, 'log': log.name}

    class RecordingExecutor(concurrent.futures.ThreadPoolExecutor):
        def shutdown(self, *args, **kwargs):
            state.events.append(('join-start', 'workers'))
            super().shutdown(*args, **kwargs)
            state.events.append(('joined', 'workers'))

    monkeypatch.setattr(validation, 'capture', capture)
    monkeypatch.setattr(validation.subprocess, 'run', docker_run)
    monkeypatch.setattr(validation, 'run_step', run_step)
    monkeypatch.setattr(validation, 'ThreadPoolExecutor', RecordingExecutor, raising=False)
    fingerprint = {'dependency_image': 'sha256:dependencies',
                   'base_images': {'python': 'python@sha256:fixed', 'postgres': 'postgres@sha256:fixed'}}

    def execute(mode='full'):
        return validation.execute_validation(source, output, fingerprint, mode, tmp_path)

    state.execute = execute
    state.output = output
    state.workspace = tmp_path
    return state


def test_full_parallel_groups_overlap_and_keep_stable_results(parallel_harness):
    state = parallel_harness
    result = state.execute()
    assert result['status'] == 'passed'
    assert state.maximum == 2
    assert [step['name'] for step in result['steps']] == [
        'contracts', 'django', 'python', 'browser', 'javascript', 'postgres', 'corpus',
        'production-build', 'production-smoke',
    ]
    starts = [name for action, name in state.events if action == 'start']
    assert starts[:2] == ['contracts', 'django']
    assert set(starts[2:4]) == {'python', 'postgres'}
    assert starts[4:] == ['browser', 'javascript', 'corpus', 'production-build', 'production-smoke']
    assert (state.workspace / 'python/owned.txt').read_text() == 'python'
    assert (state.workspace / 'postgres/owned.txt').read_text() == 'postgres'
    python_create = next(command for command in state.created if command[command.index('--name') + 1].endswith('-python'))
    postgres_create = next(command for command in state.created if command[command.index('--name') + 1].endswith('-postgres'))
    assert '--network' not in python_create
    assert not any(value.startswith('PHR_POSTGRES_TEST_URL=') for value in python_create)
    assert 'PHR_POSTGRES_TEST_URL=postgresql://phr_test:synthetic-local-only@postgres:5432/phr_test' in postgres_create


@pytest.mark.parametrize('failed', ['python', 'postgres'])
def test_parallel_failure_waits_for_sibling_and_blocks_later_groups(parallel_harness, failed):
    state = parallel_harness
    state.fail = failed
    result = state.execute()
    assert result['status'] == 'failed'
    assert ('end', 'python') in state.events and ('end', 'postgres') in state.events
    assert ('start', 'browser') not in state.events
    steps = {step['name']: step for step in result['steps']}
    assert steps[failed]['returncode'] == 7
    assert steps['browser']['status'] == steps['production-build']['status'] == 'not-run'
    assert json.loads((state.output / 'result.json').read_text()) == result


def test_parallel_cancellation_stops_owned_containers_before_join_then_database(parallel_harness, monkeypatch):
    state = parallel_harness
    state.cancelled = True
    def interrupted_wait(futures):
        assert state.both_started.wait(timeout=3)
        raise KeyboardInterrupt
    monkeypatch.setattr(validation, 'wait', interrupted_wait, raising=False)
    result = state.execute()
    assert result['status'] == 'failed'
    assert ('start', 'browser') not in state.events
    join_start = state.events.index(('join-start', 'workers'))
    joined = state.events.index(('joined', 'workers'))
    for name in ('python', 'postgres'):
        assert state.events.index(('remove', 'emr-validation-parallel-test-' + name)) < join_start
        assert state.events.index(('end', name)) < joined
    assert joined < state.events.index(('remove', 'emr-validation-parallel-test-db'))
    assert joined < state.events.index(('network-remove', 'emr-validation-parallel-test'))
    assert all(step['status'] != 'passed' for step in result['steps'] if step['name'] in ('python', 'postgres'))


def test_parallel_database_setup_failure_records_failure_and_cleans_scope(parallel_harness):
    state = parallel_harness
    state.setup_fail = True
    result = state.execute()
    assert result['status'] == 'failed'
    assert ('start', 'python') not in state.events
    assert ('start', 'browser') not in state.events
    assert next(step for step in result['steps'] if step['name'] == 'postgres')['status'] == 'failed'
    assert ('remove', 'emr-validation-parallel-test-db') in state.events
    assert ('network-remove', 'emr-validation-parallel-test') in state.events


@pytest.mark.parametrize('failed', ['python', 'postgres'])
def test_parallel_create_failure_never_starts_either_group(parallel_harness, failed):
    state = parallel_harness
    state.create_fail = failed
    result = state.execute()
    assert result['status'] == 'failed'
    assert not any(action == 'start' and name in ('python', 'postgres', 'browser') for action, name in state.events)
    assert next(step for step in result['steps'] if step['name'] == failed)['status'] == 'failed'
    assert 'synthetic create failure' in (state.output / (failed + '.log')).read_text()
    for name in ('python', 'postgres', 'db'):
        assert ('remove', 'emr-validation-parallel-test-' + name) in state.events


def test_parallel_worker_exception_preserves_sibling_evidence(parallel_harness):
    state = parallel_harness
    state.worker_error = 'postgres'
    result = state.execute()
    steps = {step['name']: step for step in result['steps']}
    assert result['status'] == 'failed'
    assert steps['python']['status'] == 'passed'
    assert steps['postgres']['status'] == 'failed'
    assert steps['postgres']['error'] == 'synthetic worker failure'
    assert steps['browser']['status'] == 'not-run'


@pytest.mark.parametrize('skipped,expected', [('python', 'passed'), ('postgres', 'failed')])
def test_parallel_groups_keep_existing_skip_policy(parallel_harness, skipped, expected):
    parallel_harness.skip = skipped
    assert parallel_harness.execute()['status'] == expected


def test_release_mode_keeps_serial_execution(parallel_harness):
    state = parallel_harness
    result = state.execute('release')
    assert result['status'] == 'passed'
    assert state.created == []
    assert [name for action, name in state.events if action == 'start'] == [
        'contracts', 'release-tests', 'corpus', 'production-build', 'production-smoke',
    ]
