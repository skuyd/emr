"""Validate committed source snapshots and retain verifiable local receipts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import tomllib
import uuid


class ValidationError(RuntimeError):
    pass


class ReuseUnavailable(ValidationError):
    """Release prerequisites cannot reuse full evidence; fresh validation may run."""


RELEASE_PATHS = {
    'VERSION', '.release-please-manifest.json', 'pyproject.toml',
    'package.json', 'package-lock.json', 'CHANGELOG.md',
}
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
HEADING = re.compile(r'^## \[([^]]+)\](?:\([^\r\n]+\))? (?:- \d{4}-\d{2}-\d{2}|\(\d{4}-\d{2}-\d{2}\))\r?$', re.MULTILINE)
EVALUATION_INPUTS = {
    'docs/verification/artifacts/phase-two-baseline-manifest.json',
    'docs/verification/artifacts/labs-extraction-scope-baseline-manifest.json',
    'docs/verification/artifacts/phase-two-annotation-adjudication-report.json',
}


def _digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()


def _run(command, *, env=None, input=None):
    result = subprocess.run(command, capture_output=True, check=False, env=env, input=input)
    if result.returncode:
        raise ValidationError(f'Command failed ({result.returncode}): {command[0]}\n{result.stderr.decode("utf-8", errors="replace")}')
    return result.stdout


def _run_runner(runner, args):
    # Candidate code receives process plumbing only, never caller API credentials
    # or WSLENV, which could forward credentials into the Linux process.
    allowed = {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR',
               'TEMP', 'TMP', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH'}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    return _run(_runner_command(runner, args), env=environment)


def _git(repo, *args):
    return _run(['git', '-C', str(repo), *args])


def _documentation_path(name):
    if name.startswith('docs/deployment/local/') or name in EVALUATION_INPUTS:
        return False
    if name in {'README.md', 'AGENTS.md', 'CHANGELOG.md', 'docs/document-registry.json',
                'docs/verification/traceability.json', 'docs/verification/release-evidence.json',
                '.github/pull_request_template.md'}:
        return True
    if name.endswith('.md') and name.startswith(('docs/', '.github/PULL_REQUEST_TEMPLATE/', '.github/ISSUE_TEMPLATE/')):
        return True
    parent, _, filename = name.rpartition('/')
    return ((parent == 'docs/verification/artifacts' and filename.endswith(('.json', '.xml')))
            or (parent == 'docs/verification/attestations' and filename.endswith('.json')))


def _changed_entries(repo, before, after):
    # Disable rename folding so a source deletion cannot hide behind a safe destination.
    fields = _git(repo, 'diff', '--raw', '--no-renames', '-z', before, after, '--').split(b'\0')
    entries = []
    for index in range(0, len(fields) - 1, 2):
        old_mode, new_mode, _, _, status = fields[index].decode('ascii').removeprefix(':').split()
        entries.append((fields[index + 1].decode('utf-8'), old_mode, new_mode, status))
    return entries


def _documentation_change(entry):
    name, old_mode, new_mode, status = entry
    modes = {'A': ('000000', '100644'), 'D': ('100644', '000000'), 'M': ('100644', '100644')}
    return _documentation_path(name) and modes.get(status) == (old_mode, new_mode)


def select_validation_mode(repo: Path, before: str, after: str) -> str:
    """Use docs checks only for a proven nonempty documentation-only commit diff."""
    try:
        before = _git(repo, 'rev-parse', '--verify', f'{before}^{{commit}}').decode().strip()
        after = _git(repo, 'rev-parse', '--verify', f'{after}^{{commit}}').decode().strip()
        _git(repo, 'merge-base', '--is-ancestor', before, after)
        entries = _changed_entries(repo, before, after)
        return 'docs' if entries and all(_documentation_change(entry) for entry in entries) else 'full'
    except (ValidationError, ValueError, IndexError):
        return 'full'


def _runner_command(runner, args):
    if os.name != 'nt':
        return [sys.executable, str(runner), *map(str, args)]
    prefix = ['wsl.exe', '-d', 'Ubuntu-24.04', '--exec']

    def linux_path(path):
        return _run([*prefix, 'wslpath', '-a', str(Path(path).resolve())]).decode().strip()

    converted = [linux_path(arg) if isinstance(arg, Path) else str(arg) for arg in args]
    return [*prefix, 'python3', linux_path(runner), *converted]


def _write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _check_archive(repo, revision, archive):
    expected = {}
    for entry in _git(repo, 'ls-tree', '-rz', revision).split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, object_id = metadata.decode().split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValidationError('Candidate contains a link or unsupported Git entry')
        expected[name.decode('utf-8')] = object_id
    actual = {}
    with tarfile.open(archive) as snapshot:
        for member in snapshot:
            name = Path(member.name).name
            if (name == '.env' or (name.startswith('.env.') and not name.endswith('.example')) or member.name.startswith('docs/deployment/local/')):
                raise ValidationError(f'Private file is tracked in candidate: {member.name}')
            if member.isdir():
                continue
            if not member.isfile() or member.name not in expected or member.name in actual:
                raise ValidationError('Archive contains an unexpected source entry')
            data = snapshot.extractfile(member).read()
            algorithm = 'sha256' if len(expected[member.name]) == 64 else 'sha1'
            actual[member.name] = hashlib.new(algorithm, f'blob {len(data)}\0'.encode() + data).hexdigest()
    if actual != expected:
        raise ValidationError('Archive does not match the complete committed source tree')


def _check_result(result, fingerprint, mode):
    if not isinstance(result, dict) or not isinstance(fingerprint, dict):
        raise ValidationError('Invalid validation result or fingerprint')
    required = fingerprint.get('required_steps', {}).get(mode)
    if not isinstance(required, list) or not required or len(set(required)) != len(required):
        raise ValidationError('Fingerprint has no valid required steps')
    if result.get('status') != 'passed':
        raise ValidationError('Validation result did not pass')
    if result.get('fingerprint') != fingerprint or result.get('mode') != mode:
        raise ValidationError('Execution environment or mode changed after preflight')
    steps = result.get('steps')
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ValidationError('Validation result has no steps')
    names = [step.get('name') for step in steps]
    if len(set(names)) != len(names) or set(names) != set(required):
        raise ValidationError('Validation result has missing or unexpected steps')
    if any(step.get('status') != 'passed' or step.get('returncode', 0) != 0 for step in steps):
        raise ValidationError('Validation result has failed or skipped steps')


def _export_git_pack(repo, revision, destination):
    # Explicit objects only: never traverse commit parents or include deleted files.
    objects = {revision, _git(repo, 'rev-parse', f'{revision}^{{tree}}').decode().strip()}
    for entry in _git(repo, 'ls-tree', '-rzt', revision).split(b'\0'):
        if entry:
            objects.add(entry.split(b'\t', 1)[0].decode().split()[2])
    payload = ('\n'.join(sorted(objects)) + '\n').encode('ascii')
    destination.write_bytes(_run(['git', '-C', str(repo), 'pack-objects', '--stdout',
                                 '--window=0', '--no-reuse-delta'], input=payload))


def _artifact_digests(output):
    artifacts = {}
    for path in sorted(output.rglob('*')):
        if path.is_symlink():
            raise ValidationError('Validation evidence must not contain symbolic links')
        if path.is_file() and path != output / 'result.json':
            artifacts[path.relative_to(output).as_posix()] = _digest(path.read_bytes())
    return artifacts


def _read_receipt(path):
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
        seal = receipt.pop('receipt_digest')
        if seal != _digest(receipt) or receipt.get('status') != 'passed':
            raise ValidationError('Invalid receipt digest or status')
        receipt['receipt_digest'] = seal
        artifact = Path(receipt['result_path']).read_bytes()
        if receipt['result_digest'] != _digest(artifact):
            raise ValidationError('Validation result digest changed')
        if receipt['artifact_digests'] != _artifact_digests(Path(receipt['result_path']).parent):
            raise ValidationError('Validation evidence files are missing or changed')
        _check_result(json.loads(artifact), receipt['environment'], receipt['mode'])
        return receipt
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValidationError(f'Cannot verify validation receipt: {path}') from error


def _release_diff(repo, before, after):
    entries = _changed_entries(repo, before, after)
    if not entries or any(entry[0] not in RELEASE_PATHS and not _documentation_change(entry) for entry in entries):
        raise ValidationError('Release reuse requires only version, changelog and safe documentation changes')

    def source(revision, name):
        entry = _git(repo, 'ls-tree', revision, '--', name).decode()
        if not entry.startswith('100644 blob '):
            raise ValidationError(f'Release metadata must remain a regular file: {name}')
        return _git(repo, 'show', f'{revision}:{name}').decode('utf-8')

    old = {name: source(before, name) for name in RELEASE_PATHS}
    new = {name: source(after, name) for name in RELEASE_PATHS}
    versions = []
    for files in (old, new):
        value = files['VERSION'].strip().removesuffix(' # x-release-please-version')
        if not VERSION.fullmatch(value):
            raise ValidationError('Release version must use MAJOR.MINOR.PATCH')
        versions.append(value)
    if tuple(map(int, versions[1].split('.'))) <= tuple(map(int, versions[0].split('.'))):
        raise ValidationError('Release version must increase')
    if old['VERSION'].replace(versions[0], '<version>', 1) != new['VERSION'].replace(versions[1], '<version>', 1):
        raise ValidationError('VERSION has changes beyond the product version')
    locations = {
        '.release-please-manifest.json': [('.',)],
        'pyproject.toml': [('project', 'version')],
        'package.json': [('version',)],
        'package-lock.json': [('version',), ('packages', '', 'version')],
    }
    try:
        for name, keys in locations.items():
            parsed = []
            for files, expected in zip((old, new), versions):
                document = tomllib.loads(files[name]) if name.endswith('.toml') else json.loads(files[name])
                for location in keys:
                    parent = document
                    for key in location[:-1]:
                        parent = parent[key]
                    if parent.pop(location[-1]) != expected:
                        raise ValidationError(f'Inconsistent release version in {name}')
                parsed.append(document)
            if parsed[0] != parsed[1]:
                raise ValidationError(f'Non-version content changed in {name}')
    except (ValueError, TypeError, KeyError) as error:
        raise ValidationError('Invalid release metadata') from error
    old_headings = list(HEADING.finditer(old['CHANGELOG.md']))
    new_headings = list(HEADING.finditer(new['CHANGELOG.md']))
    if not old_headings or len(new_headings) != len(old_headings) + 1 or new_headings[0][1] != versions[1]:
        raise ValidationError('Changelog must add exactly one new release')
    old_text, new_text = old['CHANGELOG.md'], new['CHANGELOG.md']
    if old_text[:old_headings[0].start()] != new_text[:new_headings[0].start()] or old_text[old_headings[0].start():] != new_text[new_headings[1].start():]:
        raise ValidationError('Changelog existing content changed')


def _matching_full_receipt(repo, path, policy, fingerprint):
    baseline = _read_receipt(path)
    if baseline['mode'] != 'full' or baseline['policy_digest'] != policy or baseline['environment'] != fingerprint:
        raise ValidationError('Full baseline policy or environment does not match')
    actual_tree = _git(repo, 'rev-parse', f'{baseline["revision"]}^{{tree}}').decode().strip()
    if actual_tree != baseline['tree']:
        raise ValidationError('Full baseline tree does not match Git')
    return baseline


def _release_baseline(repo, revision, state_dir, supplied, policy, fingerprint):
    candidates = ([Path(supplied)] if supplied is not None else []) + sorted((state_dir / 'cache').glob('*.json'))
    reason = 'No intact full baseline receipt is available'
    for path in candidates:
        try:
            baseline = _matching_full_receipt(repo, path, policy, fingerprint)
            _release_diff(repo, baseline['revision'], revision)
            return baseline, path
        except (ValidationError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            reason = str(error)
    raise ReuseUnavailable(f'Release validation reuse unavailable: {reason}')


def validate_revision(repo: Path, revision: str, state_dir: Path, *, mode='full', baseline_receipt: Path | None = None, base_revision: str | None = None) -> dict:
    """Validate a commit; docs requires a safe diff and release requires full proof."""
    try:
        return _validate_revision(Path(repo), revision, Path(state_dir), mode, baseline_receipt, base_revision)
    except ValidationError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValidationError(f'Validation could not complete: {error}') from error


def _validate_revision(repo, revision, state_dir, mode, baseline_receipt, base_revision):
    started = time.monotonic()
    if mode not in ('full', 'release', 'docs'):
        raise ValidationError('Unknown validation mode')
    revision = _git(repo, 'rev-parse', '--verify', f'{revision}^{{commit}}').decode().strip()
    if mode == 'docs':
        if base_revision is None or select_validation_mode(repo, base_revision, revision) != 'docs':
            raise ValidationError('Docs validation requires a nonempty safe documentation-only diff from an ancestor')
        base_revision = _git(repo, 'rev-parse', '--verify', f'{base_revision}^{{commit}}').decode().strip()
    tree = _git(repo, 'rev-parse', f'{revision}^{{tree}}').decode().strip()
    run_dir = state_dir.resolve() / 'runs' / uuid.uuid4().hex
    run_dir.mkdir(parents=True)
    archive = run_dir / 'source.tar'
    _git(repo, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
         'archive', '--format=tar', f'--output={archive}', revision)
    _check_archive(repo, revision, archive)
    git_pack = run_dir / 'source.pack'
    _export_git_pack(repo, revision, git_pack)
    runner_source = _git(repo, 'show', f'{revision}:tools/local_validation.py')
    runner = run_dir / 'local_validation.py'
    runner.write_bytes(runner_source)
    fingerprint = json.loads(_run_runner(runner, ['--fingerprint', '--archive', archive, '--mode', mode]))
    if not isinstance(fingerprint, dict) or not fingerprint.get('command_digest'):
        raise ValidationError('Fingerprint is missing command digest')
    policy = _digest({'engine': _digest(Path(__file__).read_bytes()), 'runner': _digest(runner_source)})
    baseline = None
    baseline_path = None
    if mode == 'release':
        baseline, baseline_path = _release_baseline(repo, revision, state_dir.resolve(), baseline_receipt, policy, fingerprint)
    cache_key = _digest({'tree': tree, 'environment': fingerprint, 'policy': policy, 'mode': mode, 'baseline': baseline['receipt_digest'] if baseline else None})
    cache = state_dir.resolve() / 'cache' / f'{cache_key}.json'
    prior = None
    if cache.is_file():
        try:
            prior = _read_receipt(cache)
            if prior['cache_key'] != cache_key or prior['tree'] != tree or prior['environment'] != fingerprint or prior['policy_digest'] != policy or prior['mode'] != mode:
                prior = None
        except ValidationError:
            prior = None
    if prior is None and mode == 'full':
        for path in sorted((state_dir.resolve() / 'cache').glob('*.json')):
            try:
                document_baseline = _matching_full_receipt(repo, path, policy, fingerprint)
                if select_validation_mode(repo, document_baseline['revision'], revision) != 'docs':
                    continue
            except (ValidationError, OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            print(f'Reusing full validation from {document_baseline["revision"][:12]}; validating documentation changes', flush=True)
            receipt = _validate_revision(repo, revision, state_dir, 'docs', None, document_baseline['revision'])
            receipt.update(business_reused=True, baseline_revision=document_baseline['revision'], baseline_receipt=str(path),
                           baseline_digest=document_baseline['receipt_digest'], baseline_selection='cache', seconds=time.monotonic() - started)
            receipt.pop('receipt_digest')
            receipt['receipt_digest'] = _digest(receipt)
            _write_json(Path(receipt['receipt_path']), receipt)
            _write_json(state_dir.resolve() / 'cache' / f'{receipt["cache_key"]}.json', receipt)
            return receipt
    if prior is None:
        output = run_dir / 'output'
        output.mkdir()
        try:
            _run_runner(runner, ['--archive', archive, '--git-pack', git_pack, '--revision', revision,
                                 '--output', output, '--mode', mode])
            result_path = output / 'result.json'
            artifact = result_path.read_bytes()
            result = json.loads(artifact)
            _check_result(result, fingerprint, mode)
        except ValidationError as error:
            raise ValidationError(f'{error}\nValidation evidence directory: {output}') from error
        result_digest = _digest(artifact)
        artifact_digests = _artifact_digests(output)
    else:
        result_path = Path(prior['result_path'])
        result_digest = prior['result_digest']
        artifact_digests = prior['artifact_digests']
    receipt_path = run_dir / 'receipt.json'
    receipt = {
        'schema': 1, 'status': 'passed', 'revision': revision, 'tree': tree,
        'mode': mode, 'environment': fingerprint, 'policy_digest': policy,
        'command_digest': fingerprint['command_digest'], 'cache_key': cache_key,
        'result_path': str(result_path), 'result_digest': result_digest,
        'artifact_digests': artifact_digests,
        'receipt_path': str(receipt_path), 'reused': prior is not None,
        'validated_revision': prior['validated_revision'] if prior else revision,
        'seconds': time.monotonic() - started, 'business_reused': baseline is not None,
    }
    if mode == 'docs':
        receipt['base_revision'] = base_revision
    if baseline:
        receipt.update(baseline_revision=baseline['revision'], baseline_receipt=str(baseline_path), baseline_digest=baseline['receipt_digest'],
                       baseline_selection='supplied' if baseline_receipt is not None and baseline_path == Path(baseline_receipt) else 'cache')
    receipt['receipt_digest'] = _digest(receipt)
    _write_json(receipt_path, receipt)
    cache.parent.mkdir(parents=True, exist_ok=True)
    _write_json(cache, receipt)
    return receipt
