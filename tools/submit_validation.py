"""Validate committed source snapshots and retain verifiable local receipts."""

from __future__ import annotations

import ast
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
    """Release prerequisites have no trustworthy compatible validation evidence."""


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
WORKFLOW_SOURCES = {'tools/submit.py', 'tools/submit_validation.py', 'tools/local_validation.py'}
WORKFLOW_TESTS = {'tests/tools/test_submit.py', 'tests/tools/test_submit_validation.py', 'tests/tools/test_local_validation.py'}
WINDOWS_ONLY_TESTS = {'tests/deploy/test_start_local_script.py'}
WINDOWS_ONLY_INPUTS = {'deploy/start-local.ps1', *WINDOWS_ONLY_TESTS}
RETIRED_GLUCOSE_TOOLS = {
    'tools/glucose_pipeline_evaluation.py', 'tools/glucose_source_evaluation.py',
    'tools/glucose_source_mapping.py',
}
RETIRED_TREATMENT_TOOLS = {'tools/treatment_cycle_evaluation.py', 'tools/treatment_source_mapping.py'}
DOCS_GROUPS = ['documentation', 'traceability', 'release-gate', 'version']
FULL_GROUPS = ['contracts', 'django', 'python', 'browser', 'javascript', 'postgres', 'corpus',
               'production-build', 'production-smoke']
TARGET_GROUPS = ('python', 'browser', 'postgres', 'javascript')
TARGET_STEP_ORDER = ('python', 'browser', 'javascript', 'postgres')
STATIC_DESIGN_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.pdf')


def _digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()


def validation_plan_digest(plan, candidate_revision=None):
    if candidate_revision is None or candidate_revision == plan['revision']:
        return _digest(plan)
    return _digest({'plan': plan, 'candidate_revision': candidate_revision})


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
    if name.startswith('prototype-gallery/screenshots/') and name.lower().endswith(STATIC_DESIGN_SUFFIXES):
        return True
    if name.startswith('docs/verification/artifacts/feature-pruning-browser/') and name.lower().endswith('.png'):
        return True
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


def select_validation_plan(repo: Path, before: str, after: str) -> dict:
    """Derive the required checks from a complete, proven commit diff."""
    plan = {'mode': 'blocked', 'groups': [], 'reason': '', 'changed_paths': [],
            'base_revision': before, 'revision': after,
            'targets': {group: [] for group in TARGET_GROUPS},
            'full_recommended': False, 'risks': []}
    try:
        before = _git(repo, 'rev-parse', '--verify', f'{before}^{{commit}}').decode().strip()
        after = _git(repo, 'rev-parse', '--verify', f'{after}^{{commit}}').decode().strip()
        plan.update(base_revision=before, revision=after)
        _git(repo, 'merge-base', '--is-ancestor', before, after)
        entries = _changed_entries(repo, before, after)
    except (ValidationError, ValueError, IndexError):
        plan['reason'] = 'Cannot prove a complete diff from an ancestor commit'
        return plan
    plan['changed_paths'] = [entry[0] for entry in entries]
    if not entries:
        plan['reason'] = 'No changed inputs; a prior receipt or a nonempty comparison is required'
        return plan
    if all(_documentation_change(entry) for entry in entries):
        plan.update(mode='docs', groups=list(DOCS_GROUPS), reason='Only documentation and evidence records changed')
        return plan
    remaining = [entry for entry in entries if not _documentation_change(entry)]
    windows_inputs = [name for name, *_ in remaining if name in WINDOWS_ONLY_INPUTS]
    if windows_inputs:
        plan['reason'] = 'Linux validation cannot execute the required Windows launcher checks for: ' + ', '.join(windows_inputs)
        return plan
    unsupported = [name for name, old_mode, new_mode, status in remaining
                   if (old_mode, new_mode, status) not in {
                       ('000000', '100644', 'A'), ('100644', '000000', 'D'), ('100644', '100644', 'M')}
                   or name in WORKFLOW_SOURCES | WORKFLOW_TESTS and status == 'D']
    if unsupported:
        plan['reason'] = 'Impact is not established for these file operations: ' + ', '.join(unsupported)
        return plan
    test_paths = _committed_test_paths(repo, after)
    targets = {group: set() for group in TARGET_GROUPS}
    groups = set()
    unknown = []
    reasons = []
    risks = []
    for name, _, _, status in remaining:
        matched, added, required, risk, reason = _targets_for_change(repo, after, name, test_paths, status)
        if not matched or (not any(added.values()) and 'workflow' not in required and 'corpus' not in required):
            unknown.append(name)
            continue
        for group in TARGET_GROUPS:
            targets[group].update(added[group])
        groups.update(required)
        if risk:
            risks.append(risk)
        reasons.append(reason)
    if unknown:
        plan['reason'] = 'Impact or executable related tests are not established for: ' + ', '.join(unknown)
        return plan
    if any(_documentation_change(entry) for entry in entries):
        groups.add('contracts')
    groups.update(group for group in TARGET_STEP_ORDER if targets[group])
    ordered = [group for group in FULL_GROUPS if group in groups]
    if 'workflow' in groups:
        ordered.insert(ordered.index('contracts') + 1, 'workflow')
    plan.update(mode='planned', groups=ordered,
                reason='; '.join(dict.fromkeys(reasons)),
                targets={group: sorted(targets[group]) for group in TARGET_GROUPS},
                full_recommended=bool(risks), risks=list(dict.fromkeys(risks)))
    return plan


def _committed_test_paths(repo, revision):
    names = _git(repo, 'ls-tree', '-r', '--name-only', revision, '--', 'tests', 'prototype-gallery').decode().splitlines()
    return {name for name in names
            if (name.startswith('tests/') or name.startswith('prototype-gallery/') and '/tests/' in name)
            and (Path(name).name.startswith('test_') and name.endswith('.py') or name.endswith('.test.mjs'))}


def _test_groups(repo, revision, name):
    if name.startswith('tests/browser/'):
        return ('browser',)
    if name.endswith('.test.mjs'):
        return ('javascript',)
    if name.endswith('_postgres.py'):
        return ('postgres',)
    source = _git(repo, 'show', f'{revision}:{name}').decode('utf-8')
    if 'pytest.mark.postgres' not in source:
        return ('python',)
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return ('python', 'postgres')
    for statement in body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'pytestmark'
                                                     for target in statement.targets):
            if 'pytest.mark.postgres' in ast.unparse(statement.value):
                return ('postgres',)
    return ('python', 'postgres')


def _referencing_tests(repo, revision, test_paths, needle):
    try:
        output = _git(repo, 'grep', '-l', '-F', needle, revision, '--', 'tests', 'prototype-gallery').decode().splitlines()
    except ValidationError:
        return set()
    # Workflow fixtures contain application paths as selection examples, not consumers.
    return {name.split(':', 1)[1] for name in output
            if ':' in name and name.split(':', 1)[1] in test_paths - WORKFLOW_TESTS}


def _module_route_prefixes(repo, revision, module):
    prefixes = set()
    for name in ('config/urls.py', f'apps/{module}/urls.py'):
        try:
            source = _git(repo, 'show', f'{revision}:{name}').decode('utf-8')
        except ValidationError:
            continue
        if name == 'config/urls.py':
            pattern = r'path\([\'\"]([^\'\"]+)/[\'\"],\s*include\([\'\"]apps\.' + re.escape(module) + r'\.'
        else:
            pattern = r'path\([\'\"]([a-z][a-z0-9-]*)/'
        prefixes.update(re.findall(pattern, source))
    return prefixes


def _page_tests(test_paths, name):
    key = Path(name).stem.replace('-', '_').strip('_')
    selected = {path for path in test_paths
                if path.startswith(('tests/accessibility/', 'tests/ui/', 'tests/browser/'))
                and f'_{key}_' in f'_{Path(path).stem}_'}
    # These page checks have shared/acceptance names instead of page names.
    aliases = {
        'home': {'tests/browser/test_shell_browser.py'},
        'login': {'tests/accessibility/test_shell_markup.py', 'tests/browser/test_ac00_ac01_browser.py'},
    }
    selected.update(test_paths & aliases.get(key, set()))
    return selected


def _template_tests(repo, revision, name, test_paths):
    module = name.split('/')[1]
    selected = {path for path in test_paths if path.startswith(f'tests/{module}/')}
    for reference in (f'templates/{module}/', name, Path(name).name):
        selected.update(_referencing_tests(repo, revision, test_paths, reference))
    key = module.rstrip('s').replace('-', '_')
    selected.update(path for path in test_paths if path.startswith('tests/browser/') and key in path)
    selected.update(_page_tests(test_paths, name))
    return selected


def _retired_feature_for_deletion(name):
    if name == 'static/css/glucose.css' or name in RETIRED_GLUCOSE_TOOLS:
        return 'glucose'
    if (name == 'static/css/treatments.css' or name.startswith(('templates/treatments/', 'tests/treatments/'))
            or name in RETIRED_TREATMENT_TOOLS):
        return 'treatments'
    if name == 'static/css/trend.css':
        return 'trend'
    return ''


def _targets_for_change(repo, revision, name, test_paths, status):
    added = {group: set() for group in TARGET_GROUPS}
    required = set()
    risk = ''
    reason = ''
    selected = set()
    retired = _retired_feature_for_deletion(name) if status == 'D' else ''
    if retired:
        replacements = {
            'glucose': ('tests/glucose/test_removed_feature.py', 'tests/glucose/test_migrations.py'),
            'treatments': ('tests/exports/test_removed_treatments.py', 'tests/exports/test_treatment_removal_migration.py'),
            'trend': ('tests/documents/test_removed_trend_routes.py', 'tests/browser/test_lab_navigation_browser.py'),
        }[retired]
        if not set(replacements) <= test_paths:
            return False, added, required, risk, reason
        selected.update(replacements)
        required.add('django')
        risk = f'Retired {retired} files can affect callers beyond the surviving removal tests: {name}'
        reason = f'Surviving {retired} removal tests cover {name}'
    elif name == 'tools/verify_traceability.py' and status == 'M':
        consumer = 'tests/acceptance/test_traceability.py'
        if consumer not in test_paths or consumer not in _referencing_tests(repo, revision, test_paths, 'verify_traceability'):
            return False, added, required, risk, reason
        selected.add(consumer)
        required.add('contracts')
        reason = f'Traceability contract and acceptance tests cover {name}'
    elif name in {'tests/e2e/phr-v1.spec.ts', 'tests/e2e/health-home-warm-ui.spec.ts'} and status == 'M':
        consumer = {'tests/e2e/phr-v1.spec.ts': 'tests/deploy/test_release_artifacts.py',
                    'tests/e2e/health-home-warm-ui.spec.ts': 'tests/accessibility/test_task9_runtime_contract.py'}[name]
        if consumer not in test_paths or consumer not in _referencing_tests(repo, revision, test_paths, Path(name).name):
            return False, added, required, risk, reason
        selected.add(consumer)
        if 'tests/browser/test_shell_browser.py' in test_paths:
            selected.add('tests/browser/test_shell_browser.py')
        risk = f'TypeScript E2E and external Chrome/Edge/Safari are not run by this validator, even in full mode: {name}'
        reason = f'Source contract and shell browser checks cover the available part of {name}'
    elif name in WORKFLOW_SOURCES | WORKFLOW_TESTS:
        required.update(('contracts', 'workflow'))
        if name in WORKFLOW_SOURCES:
            required.update(('production-build', 'production-smoke'))
        reason = f'Workflow checks cover {name}'
    elif name in test_paths:
        selected.add(name)
        reason = f'Changed test runs directly: {name}'
    elif name.startswith('apps/') and len(name.split('/')) >= 3:
        module = name.split('/')[1]
        selected.update(path for path in test_paths if path.startswith(f'tests/{module}/'))
        selected.update(_referencing_tests(repo, revision, test_paths, f'apps.{module}'))
        selected.update(_referencing_tests(repo, revision, test_paths, f'apps/{module}/'))
        selected.update(_referencing_tests(repo, revision, test_paths, f'/{module}/'))
        for prefix in _module_route_prefixes(repo, revision, module):
            selected.update(_referencing_tests(repo, revision, test_paths, f'/{prefix}/'))
        browser_key = module.rstrip('s')
        selected.update(path for path in test_paths if path.startswith('tests/browser/')
                        and browser_key in Path(path).stem)
        required.add('django')
        if '/migrations/' in name or Path(name).name in {'permissions.py', 'policies.py'} or module == 'core':
            risk = f'Shared, permission or database impact may extend beyond selected tests: {name}'
        reason = f'Module {module} and directly referencing tests cover {name}'
    elif name.startswith('templates/') and name.endswith('.html'):
        module = name.split('/')[1]
        selected.update(_template_tests(repo, revision, name, test_paths))
        required.add('django')
        if module in {'components', 'base_app.html', 'base_public.html'} or name in {'templates/base_app.html', 'templates/base_public.html'}:
            selected.update(path for path in test_paths if path.startswith('tests/accessibility/'))
            selected.update(path for path in test_paths if path.startswith('tests/browser/') and 'shell' in path)
            risk = f'Shared template can affect additional pages: {name}'
        reason = f'Template and page checks cover {name}'
    elif name.startswith('static/'):
        selected.update(_referencing_tests(repo, revision, test_paths, name))
        selected.update(_referencing_tests(repo, revision, test_paths, name.removeprefix('static/')))
        # File-name references also cover Path(root) / "static" / "css" / "home.css".
        selected.update(_referencing_tests(repo, revision, test_paths, Path(name).name))
        selected.update(_page_tests(test_paths, name))
        stem = Path(name).stem.replace('-', '_')
        browser_key = '_'.join(stem.split('_')[:2]) if len(stem.split('_')) > 2 else stem
        selected.update(path for path in test_paths if path.startswith('tests/browser/') and (stem in path or browser_key in path))
        if name.startswith(('static/css/', 'static/js/')):
            try:
                uses = _git(repo, 'grep', '-l', '-F', name.removeprefix('static/'), revision, '--', 'templates').decode().splitlines()
            except ValidationError:
                uses = []
            for use in uses:
                template = use.split(':', 1)[1]
                selected.update(_template_tests(repo, revision, template, test_paths))
        if name in {'static/css/tokens.css', 'static/css/components.css', 'static/css/app-shell.css', 'static/js/app-shell.js'}:
            selected.update(path for path in test_paths if path.startswith(('tests/accessibility/', 'tests/ui/')))
            selected.update(path for path in test_paths if path.startswith('tests/browser/') and 'shell' in path)
            risk = f'Shared frontend asset can affect additional pages: {name}'
        if name.startswith('static/fonts/') or name == 'static/favicon.svg':
            selected.update(path for path in test_paths if path.startswith('tests/accessibility/') and 'shell' in path)
            selected.update(path for path in test_paths if path.startswith('tests/browser/') and 'shell' in path)
            risk = f'Shared frontend asset can affect additional pages: {name}'
        if name.startswith('static/trial/'):
            selected.update(path for path in test_paths if path in {'tests/accounts/test_synthetic_trial.py',
                                                                   'tests/test_project_configuration.py'})
        reason = f'Frontend tests referencing or covering {name}'
    elif name.startswith('prototype-gallery/') and name.endswith(('.html', '.css', '.js', '.mjs', '.svg', '.png', '.jpg', '.jpeg', '.webp')):
        section = name.split('/')[1]
        if section in {'tests', 'variants'} or '/' not in name.removeprefix('prototype-gallery/'):
            selected.update(path for path in test_paths if path.startswith('prototype-gallery/') and path.endswith('.test.mjs'))
        else:
            selected.update(path for path in test_paths if path.startswith(f'prototype-gallery/{section}/tests/')
                            or path.startswith('prototype-gallery/tests/') and section.replace('-', '_') in path)
        reason = f'Prototype checks cover {name}'
    elif name.startswith(('tests/fixtures/',)) or name in {'tests/conftest.py', 'pytest.ini', 'tools/run_required_tests.py'}:
        selected.update(path for path in test_paths if path in {
            'tests/test_project_configuration.py', 'tests/tools/test_required_tests.py',
            'tests/tools/test_local_validation.py'})
        if name.startswith('tests/fixtures/'):
            selected.update(path for path in test_paths if path.startswith(f'tests/{name.split("/")[2]}/'))
            selected.update(_referencing_tests(repo, revision, test_paths, name))
        risk = f'Shared test input can affect additional suites: {name}'
        reason = f'Test infrastructure checks cover {name}'
    elif name.startswith('tests/') and name.endswith('.py') and len(name.split('/')) >= 3:
        module = name.split('/')[1]
        selected.update(path for path in test_paths if path.startswith(f'tests/{module}/'))
        reason = f'Tests using helper in {module} cover {name}'
    elif name.startswith(('config/', 'deploy/', '.github/workflows/')) or name in {
            'manage.py', 'pyproject.toml', 'package.json', 'package-lock.json',
            'requirements-prod.lock', 'requirements-test.lock', 'compose.yaml',
            '.dockerignore', '.env.example', 'playwright.config.ts',
            'VERSION', '.release-please-manifest.json', 'release-please-config.json'}:
        config_tests = {'tests/test_project_configuration.py'}
        if name.startswith('config/') or name == 'manage.py':
            selected.update(path for path in test_paths if path.startswith('tests/deploy/'))
        elif name == '.env.example':
            pass
        elif name == 'playwright.config.ts':
            selected.update(path for path in test_paths if path.startswith('tests/browser/') and 'shell' in path)
        elif name.startswith('.github/workflows/') or name in {'VERSION', '.release-please-manifest.json',
                                                                'release-please-config.json'}:
            config_tests = {'tests/tools/test_release_automation.py', 'tests/tools/test_release_version.py'}
        else:
            config_tests = {'tests/tools/test_dependency_locks.py'}
            selected.update(path for path in test_paths if path.startswith('tests/deploy/'))
        selected.update(test_paths & config_tests)
        required.add('contracts')
        if name.startswith('config/') or name == 'manage.py':
            required.add('django')
        if name.startswith(('config/', 'deploy/')) or name in {'requirements-prod.lock', 'requirements-test.lock', 'package.json', 'package-lock.json', 'pyproject.toml', 'compose.yaml', '.dockerignore'}:
            required.update(('production-build', 'production-smoke'))
        if name.startswith(('config/', 'deploy/')) or name in {
                'manage.py', 'pyproject.toml', 'package.json', 'package-lock.json',
                'requirements-prod.lock', 'requirements-test.lock', 'compose.yaml', '.dockerignore'}:
            risk = f'Configuration or dependency impact may extend beyond selected tests: {name}'
        reason = f'Configuration and packaging checks cover {name}'
    elif name in EVALUATION_INPUTS:
        selected.update(path for path in test_paths if path in {
            'tests/tools/test_phase_two_evaluation.py', 'tests/labs/test_phase_two_release_evaluation.py'})
        required.add('corpus')
        reason = f'Evaluation checks cover {name}'
    else:
        return False, added, required, risk, reason
    unavailable = selected & WINDOWS_ONLY_TESTS
    if unavailable:
        selected.difference_update(unavailable)
        platform_risk = ('Windows-only checks are not executed by the Linux runner, including full validation: '
                         + ', '.join(sorted(unavailable)))
        risk = '; '.join(part for part in (risk, platform_risk) if part)
    for path in selected:
        for group in _test_groups(repo, revision, path):
            added[group].add(path)
    return True, added, required, risk, reason


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
        if receipt.get('command_digest') != receipt.get('environment', {}).get('command_digest'):
            raise ValidationError('Receipt command digest differs from its environment')
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


def _release_baseline(repo, revision, state_dir, supplied, policy, fingerprint, runner, archive):
    candidates = ([Path(supplied)] if supplied is not None else []) + sorted((state_dir / 'cache').glob('*.json'))
    reason = 'No intact full or planned baseline receipt is available'
    for path in candidates:
        try:
            baseline = _read_receipt(path)
            baseline_mode = baseline['mode']
            environment = lambda value: {key: item for key, item in value.items()
                                         if key not in {'required_steps', 'command_digest', 'targets'}}
            if (baseline_mode not in ('full', 'planned') or baseline['policy_digest'] != policy
                    or environment(baseline['environment']) != environment(fingerprint)):
                raise ValidationError('Baseline mode, policy or environment does not match')
            actual_tree = _git(repo, 'rev-parse', f'{baseline["revision"]}^{{tree}}').decode().strip()
            if actual_tree != baseline['tree']:
                raise ValidationError('Baseline tree does not match Git')
            if 'plan' in baseline:
                source_revision = baseline.get('plan_source_revision', baseline['revision'])
                if source_revision != baseline['revision']:
                    _release_diff(repo, source_revision, baseline['revision'])
                plan = select_validation_plan(repo, baseline['base_revision'], source_revision)
                if (plan['mode'] != 'planned' or baseline['plan'] != plan
                        or baseline['plan_digest'] != validation_plan_digest(plan, baseline['revision'])
                        or baseline.get('coverage_targets') != plan['targets']
                        or baseline.get('risks') != plan['risks']
                        or baseline.get('full_recommended') != plan['full_recommended']):
                    raise ValidationError('Baseline validation plan cannot be reproduced from Git')
                if baseline_mode == 'planned':
                    group_args = ['--groups', json.dumps(plan['groups']), '--targets', json.dumps(plan['targets'])]
                    candidate_environment = json.loads(_run_runner(runner, ['--fingerprint', '--archive', archive,
                                                                              '--mode', 'planned', *group_args]))
                    if baseline['environment'] != candidate_environment:
                        raise ValidationError('Baseline scoped commands or exact targets changed')
                elif baseline['command_digest'] != fingerprint['command_digest']:
                    raise ValidationError('Full baseline commands changed')
            elif baseline_mode == 'planned' or baseline['command_digest'] != fingerprint['command_digest']:
                raise ValidationError('Baseline commands changed')
            if baseline['revision'] != revision:
                _release_diff(repo, baseline['revision'], revision)
            elif 'plan' not in baseline or baseline.get('plan_source_revision') == baseline['revision']:
                raise ValidationError('Same-revision release baseline has no proven release-only diff')
            return baseline, path
        except (ValidationError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            reason = str(error)
    raise ReuseUnavailable(f'Release validation reuse unavailable: {reason}')


def validate_revision(repo: Path, revision: str, state_dir: Path, *, mode='full', baseline_receipt: Path | None = None,
                      base_revision: str | None = None, scope_decision: str | None = None,
                      plan_digest: str | None = None, plan_revision: str | None = None) -> dict:
    """Validate a commit; scoped checks and release reuse require proven inputs."""
    try:
        return _validate_revision(Path(repo), revision, Path(state_dir), mode, baseline_receipt, base_revision,
                                  scope_decision, plan_digest, plan_revision)
    except ValidationError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValidationError(f'Validation could not complete: {error}') from error


def _validate_revision(repo, revision, state_dir, mode, baseline_receipt, base_revision,
                       scope_decision, requested_plan_digest, plan_revision):
    started = time.monotonic()
    if mode not in ('full', 'release', 'docs', 'planned'):
        raise ValidationError('Unknown validation mode')
    revision = _git(repo, 'rev-parse', '--verify', f'{revision}^{{commit}}').decode().strip()
    plan = None
    group_args = []
    if scope_decision not in (None, 'run', 'skip') or (scope_decision == 'run' and mode != 'full') or (scope_decision == 'skip' and mode != 'planned'):
        raise ValidationError('Invalid full-test scope decision for validation mode')
    if mode in ('planned', 'full') and (mode == 'planned' or scope_decision is not None or plan_revision is not None):
        if plan_revision is None:
            plan_revision = revision
        else:
            plan_revision = _git(repo, 'rev-parse', '--verify', f'{plan_revision}^{{commit}}').decode().strip()
        if plan_revision != revision:
            _release_diff(repo, plan_revision, revision)
        plan = select_validation_plan(repo, base_revision, plan_revision) if base_revision is not None else None
        if plan is None or plan['mode'] != 'planned':
            raise ValidationError('Planned validation requires a proven diff with executable related checks: ' + (plan['reason'] if plan else 'missing base revision'))
        expected_digest = validation_plan_digest(plan, revision)
        if requested_plan_digest is not None and requested_plan_digest != expected_digest:
            raise ValidationError('Validation plan digest changed before execution')
        if plan['full_recommended']:
            if scope_decision not in ('run', 'skip') or requested_plan_digest is None:
                raise ValidationError('A bound full-test scope decision and plan digest are required')
        elif scope_decision is not None:
            raise ValidationError('Full-test scope decision is only valid for a full recommendation')
        base_revision = plan['base_revision']
        if mode == 'planned':
            group_args = ['--groups', json.dumps(plan['groups']), '--targets', json.dumps(plan['targets'])]
    elif requested_plan_digest is not None or plan_revision is not None:
        raise ValidationError('A validation plan digest or source revision requires scoped validation')
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
    fingerprint = json.loads(_run_runner(runner, ['--fingerprint', '--archive', archive, '--mode', mode, *group_args]))
    if not isinstance(fingerprint, dict) or not fingerprint.get('command_digest'):
        raise ValidationError('Fingerprint is missing command digest')
    if plan and fingerprint.get('required_steps', {}).get('planned') != plan['groups']:
        if mode == 'planned':
            raise ValidationError('Fingerprint does not match the required validation plan')
    if mode == 'planned' and fingerprint.get('targets') != plan['targets']:
        raise ValidationError('Fingerprint does not match exact validation targets')
    policy = _digest({'engine': _digest(Path(__file__).read_bytes()), 'runner': _digest(runner_source)})
    baseline = None
    baseline_path = None
    if mode == 'release':
        baseline, baseline_path = _release_baseline(repo, revision, state_dir.resolve(), baseline_receipt, policy,
                                                    fingerprint, runner, archive)
    cache_key = _digest({'tree': tree, 'environment': fingerprint, 'policy': policy, 'mode': mode,
                         'plan': plan, 'baseline': baseline['receipt_digest'] if baseline else None})
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
            receipt = _validate_revision(repo, revision, state_dir, 'docs', None, document_baseline['revision'],
                                         None, None, None)
            receipt.update(business_reused=True, validation_reused=True, baseline_mode='full', baseline_revision=document_baseline['revision'], baseline_receipt=str(path),
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
                                 '--output', output, '--mode', mode, *group_args])
            result_path = output / 'result.json'
            artifact = result_path.read_bytes()
            result = json.loads(artifact)
            _check_result(result, fingerprint, mode)
            if plan and mode == 'planned' and result.get('targets') != plan['targets']:
                raise ValidationError('Validation result does not match exact targets')
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
        'seconds': time.monotonic() - started, 'validation_reused': baseline is not None,
        'business_reused': baseline is not None and baseline['mode'] == 'full',
    }
    if mode in ('docs', 'planned') or plan:
        receipt['base_revision'] = base_revision
    if plan:
        receipt.update(plan=plan, plan_digest=validation_plan_digest(plan, revision),
                       plan_source_revision=plan_revision, scope_decision=scope_decision,
                       risks=plan['risks'], full_recommended=plan['full_recommended'],
                       coverage_targets=plan['targets'])
    if baseline:
        receipt.update(baseline_mode=baseline['mode'], baseline_revision=baseline['revision'], baseline_receipt=str(baseline_path), baseline_digest=baseline['receipt_digest'],
                       baseline_selection='supplied' if baseline_receipt is not None and baseline_path == Path(baseline_receipt) else 'cache')
        receipt.update(scope_decision=baseline.get('scope_decision'), risks=baseline.get('risks', []),
                       full_recommended=baseline.get('full_recommended', False),
                       coverage_targets=baseline.get('coverage_targets', {group: [] for group in TARGET_GROUPS}))
    receipt['receipt_digest'] = _digest(receipt)
    _write_json(receipt_path, receipt)
    cache.parent.mkdir(parents=True, exist_ok=True)
    _write_json(cache, receipt)
    return receipt
