import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools import submit_validation as validation


RUNNER = '''import argparse, json, os, tarfile
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--fingerprint', action='store_true')
p.add_argument('--archive')
p.add_argument('--output')
p.add_argument('--mode')
a = p.parse_args()
fingerprint = {'environment': os.environ.get('LANG', 'one'),
      'inherited_env_names': sorted(os.environ),
      'command_digest': 'commands-1',
      'required_steps': {'full': ['business', 'docker'], 'release': ['version', 'docker']}}
if a.fingerprint:
    print(json.dumps(fingerprint))
else:
    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(a.archive) as archive:
        files = {m.name: archive.extractfile(m).read().decode() for m in archive.getmembers() if m.isfile()}
    status_file = Path(os.environ['TMP']) / 'runner-status'
    status = status_file.read_text() if status_file.exists() else files.get('result-status', 'passed')
    names = ['business', 'docker'] if a.mode == 'full' else ['version', 'docker']
    steps = [{'name': n, 'status': status, 'returncode': 0} for n in names]
    if status == 'missing': steps = steps[:1]
    if 'environment-changed' in files: fingerprint['environment'] = 'changed-after-fingerprint'
    (out / 'browser.xml').write_text('<testsuite tests="1" failures="0"/>')
    (out / 'result.json').write_text(json.dumps({'status': 'passed', 'mode': a.mode, 'fingerprint': fingerprint, 'steps': steps, 'files': files}))
'''


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo):
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fixture')
    return git(repo, 'rev-parse', 'HEAD')


def versions(repo, version):
    (repo / 'VERSION').write_text(version + '\n')
    (repo / '.release-please-manifest.json').write_text(json.dumps({'.': version}))
    (repo / 'pyproject.toml').write_text(f'[project]\nversion = "{version}"\ndependencies = ["example==1"]\n')
    (repo / 'package.json').write_text(json.dumps({'version': version, 'scripts': {'test': 'test'}}))
    (repo / 'package-lock.json').write_text(json.dumps({'version': version, 'packages': {'': {'version': version}, 'node_modules/a': {'version': '1.0.0'}}}))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv('TMP', str(tmp_path))
    monkeypatch.setenv('LANG', 'C')
    path = tmp_path / 'repo with spaces'
    path.mkdir()
    git(path, 'init', '-q')
    git(path, 'config', 'user.email', 'test@example.invalid')
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'core.autocrlf', 'false')
    (path / 'tools').mkdir()
    (path / 'tools/local_validation.py').write_text(RUNNER)
    (path / 'app.py').write_text('committed source')
    versions(path, '1.0.0')
    (path / 'CHANGELOG.md').write_text('# Changelog\n\n## Unreleased\n\n## [1.0.0] - 2026-09-01\n\n- Existing.\n')
    commit(path)
    monkeypatch.setattr(validation, '_runner_command', lambda runner, args: [sys.executable, str(runner), *map(str, args)])
    return path


def release(repo, version='1.0.1'):
    versions(repo, version)
    path = repo / 'CHANGELOG.md'
    path.write_text(path.read_text().replace('## [1.0.0]', f'## [{version}] - 2026-09-26\n\n- New.\n\n## [1.0.0]', 1))
    return commit(repo)


def test_snapshot_uses_commit_and_reuses_identical_tree(repo, tmp_path):
    state = tmp_path / 'state'
    original = git(repo, 'rev-parse', 'HEAD')
    (repo / 'app.py').write_text('dirty secret')
    (repo / '.env').write_text('PRIVATE')
    first = validation.validate_revision(repo, original, state)
    result = json.loads(Path(first['result_path']).read_text())
    assert result['files']['app.py'] == 'committed source'
    assert '.env' not in result['files']
    git(repo, 'commit', '--allow-empty', '-qm', 'same tree')
    second = validation.validate_revision(repo, 'HEAD', state)
    assert second['reused'] is True
    assert second['revision'] != first['revision']
    assert second['validated_revision'] == original
    assert second['tree'] == first['tree']


@pytest.mark.parametrize('status', ['failed', 'skipped', 'missing'])
def test_runner_cannot_claim_pass_with_incomplete_steps(repo, tmp_path, status):
    (repo / 'result-status').write_text(status)
    commit(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', tmp_path / 'state')


def test_environment_and_artifact_changes_invalidate_cache(repo, tmp_path, monkeypatch):
    state = tmp_path / 'state'
    first = validation.validate_revision(repo, 'HEAD', state)
    monkeypatch.setenv('LANG', 'C.UTF-8')
    second = validation.validate_revision(repo, 'HEAD', state)
    assert second['reused'] is False
    Path(second['result_path']).write_text('{}')
    third = validation.validate_revision(repo, 'HEAD', state)
    assert third['reused'] is False
    assert first['environment'] != second['environment']


def test_release_reuses_business_evidence_with_explicit_provenance(repo, tmp_path):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    revision = release(repo)
    result = validation.validate_revision(repo, revision, state, mode='release', baseline_receipt=Path(baseline['receipt_path']))
    assert result['revision'] == revision
    assert result['baseline_revision'] == baseline['revision']
    assert result['mode'] == 'release'
    assert result['business_reused'] is True
    assert [s['name'] for s in json.loads(Path(result['result_path']).read_text())['steps']] == ['version', 'docker']


@pytest.mark.parametrize('path,transform', [
    ('package.json', lambda s: s.replace('"test": "test"', '"test": "skip"')),
    ('package-lock.json', lambda s: s.replace('"version": "1.0.0"', '"version": "9.0.0"')),
    ('pyproject.toml', lambda s: s.replace('example==1', 'example==2')),
    ('.release-please-manifest.json', lambda s: s[:-1] + ', "other": "1.0.1"}'),
    ('CHANGELOG.md', lambda s: s.replace('Existing.', 'Rewritten.')),
    ('app.py', lambda s: 'changed code'),
])
def test_release_rejects_non_version_changes(repo, tmp_path, path, transform):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    release(repo)
    target = repo / path
    target.write_text(transform(target.read_text()))
    commit(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=Path(baseline['receipt_path']))


@pytest.mark.parametrize('version', ['1.0.0', '0.9.0', '01.0.1'])
def test_release_requires_increasing_valid_version(repo, tmp_path, version):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    release(repo, version)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=Path(baseline['receipt_path']))


def test_release_rejects_corrupt_baseline(repo, tmp_path):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    Path(baseline['result_path']).unlink()
    release(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=Path(baseline['receipt_path']))


def test_execution_environment_must_match_preflight(repo, tmp_path):
    (repo / 'environment-changed').write_text('true')
    commit(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', tmp_path / 'state')


@pytest.mark.parametrize('private', ['.env', 'docs/deployment/local/tencent-cloud/trial-access.txt'])
def test_tracked_private_files_are_rejected(repo, tmp_path, private):
    target = repo / private
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('private')
    commit(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', tmp_path / 'state')


def test_modified_receipt_cannot_be_release_baseline(repo, tmp_path):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    receipt_path = Path(baseline['receipt_path'])
    receipt = json.loads(receipt_path.read_text())
    receipt['tree'] = 'forged'
    receipt_path.write_text(json.dumps(receipt))
    release(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=receipt_path)


def test_runner_change_cannot_reuse_previous_results(repo, tmp_path):
    state = tmp_path / 'state'
    first = validation.validate_revision(repo, 'HEAD', state)
    (repo / 'tools/local_validation.py').write_text(RUNNER + '\n# Changed command policy\n')
    commit(repo)
    second = validation.validate_revision(repo, 'HEAD', state)
    assert second['reused'] is False
    assert second['policy_digest'] != first['policy_digest']


@pytest.mark.parametrize('attributes', ['app.py export-ignore\n', 'app.py export-subst\n'])
def test_git_archive_cannot_hide_or_rewrite_committed_inputs(repo, tmp_path, attributes):
    (repo / '.gitattributes').write_text(attributes)
    (repo / 'app.py').write_text('$Format:%H$')
    commit(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', tmp_path / 'state')


def test_malformed_fingerprint_raises_validation_error(repo, tmp_path):
    (repo / 'tools/local_validation.py').write_text(RUNNER.replace('print(json.dumps(fingerprint))', "print('[]')"))
    commit(repo)
    with pytest.raises(validation.ValidationError):
        validation.validate_revision(repo, 'HEAD', tmp_path / 'state')


@pytest.mark.parametrize('reason', ['absent', 'corrupt', 'environment', 'source'])
def test_release_prerequisite_failure_is_distinct_from_execution_failure(repo, tmp_path, monkeypatch, reason):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    release(repo)
    path = Path(baseline['receipt_path'])
    if reason == 'absent':
        path = None
    elif reason == 'corrupt':
        Path(baseline['result_path']).unlink()
    elif reason == 'environment':
        monkeypatch.setenv('LANG', 'changed')
    else:
        (repo / 'app.py').write_text('changed business code')
        commit(repo)
    with pytest.raises(validation.ValidationError) as caught:
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=path)
    assert type(caught.value).__name__ == 'ReuseUnavailable'


def test_release_execution_failure_is_not_eligible_for_full_fallback(repo, tmp_path, monkeypatch):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    release(repo)
    (tmp_path / 'runner-status').write_text('failed')
    with pytest.raises(validation.ValidationError) as caught:
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=baseline['receipt_path'])
    assert type(caught.value) is validation.ValidationError


def test_candidate_runner_never_inherits_credentials_or_wslenv(repo, tmp_path, monkeypatch):
    sensitive = ['GH_TOKEN', 'GITHUB_TOKEN', 'RELEASE_PLEASE_TOKEN', 'OPENAI_API_KEY']
    for name in sensitive:
        monkeypatch.setenv(name, 'synthetic-test-value')
    monkeypatch.setenv('WSLENV', 'GH_TOKEN/u:OPENAI_API_KEY:PATH/p')
    result = validation.validate_revision(repo, 'HEAD', tmp_path / 'state')
    environment_names = result['environment']['inherited_env_names']
    assert not set(sensitive + ['WSLENV']).intersection(environment_names)
    assert 'PATH' in environment_names


@pytest.mark.parametrize('mutation', ['delete', 'modify'])
def test_missing_or_changed_report_invalidates_cached_pass(repo, tmp_path, mutation):
    state = tmp_path / 'state'
    first = validation.validate_revision(repo, 'HEAD', state)
    report = Path(first['result_path']).parent / 'browser.xml'
    if mutation == 'delete':
        report.unlink()
    else:
        report.write_text('<testsuite tests="0"/>')
    second = validation.validate_revision(repo, 'HEAD', state)
    assert second['reused'] is False
    assert 'browser.xml' in second['artifact_digests']


def test_missing_baseline_report_prevents_release_reuse(repo, tmp_path):
    state = tmp_path / 'state'
    baseline = validation.validate_revision(repo, 'HEAD', state)
    (Path(baseline['result_path']).parent / 'browser.xml').unlink()
    release(repo)
    with pytest.raises(validation.ReuseUnavailable):
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=baseline['receipt_path'])
