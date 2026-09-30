import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import submit_validation as validation
from tools import local_validation as local_runner


RUNNER = '''import argparse, json, os, tarfile
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--fingerprint', action='store_true')
p.add_argument('--archive')
p.add_argument('--output')
p.add_argument('--mode')
p.add_argument('--git-pack')
p.add_argument('--revision')
p.add_argument('--groups')
p.add_argument('--targets')
a = p.parse_args()
fingerprint = {'environment': os.environ.get('LANG', 'one'),
      'inherited_env_names': sorted(os.environ),
      'command_digest': 'commands-1',
      'required_steps': {'full': ['business', 'docker'], 'release': ['version', 'docker'],
                         'docs': ['documentation', 'traceability', 'release-gate', 'version']}}
if a.mode == 'docs': fingerprint['environment'] += '-docs'
if a.mode == 'planned': fingerprint['required_steps']['planned'] = json.loads(a.groups)
if a.mode == 'planned': fingerprint['targets'] = json.loads(a.targets)
if a.fingerprint:
    print(json.dumps(fingerprint))
else:
    assert a.revision and Path(a.git_pack).read_bytes().startswith(b'PACK')
    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(a.archive) as archive:
        files = {m.name: archive.extractfile(m).read().decode() for m in archive.getmembers() if m.isfile()}
    status_file = Path(os.environ['TMP']) / 'runner-status'
    status = status_file.read_text() if status_file.exists() else files.get('result-status', 'passed')
    names = fingerprint['required_steps'][a.mode]
    steps = [{'name': n, 'status': status, 'returncode': 0} for n in names]
    if status == 'missing': steps = steps[:1]
    if 'environment-changed' in files: fingerprint['environment'] = 'changed-after-fingerprint'
    (out / 'browser.xml').write_text('<testsuite tests="1" failures="0"/>')
    (out / 'result.json').write_text(json.dumps({'status': 'passed', 'mode': a.mode, 'fingerprint': fingerprint, 'steps': steps, 'files': files,
                                                 'targets': json.loads(a.targets) if a.targets else None}))
    if status == 'process-error':
        print('synthetic-hidden-stdout')
        raise SystemExit(1)
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


@pytest.mark.parametrize('name,mode,groups', [
    ('README.md', 'docs', ['documentation', 'traceability', 'release-gate', 'version']),
    ('tools/submit.py', 'planned', ['contracts', 'workflow', 'production-build', 'production-smoke']),
    ('tools/submit_validation.py', 'planned', ['contracts', 'workflow', 'production-build', 'production-smoke']),
    ('tools/local_validation.py', 'planned', ['contracts', 'workflow', 'production-build', 'production-smoke']),
    ('tests/tools/test_submit.py', 'planned', ['contracts', 'workflow']),
    ('tests/tools/test_submit_validation.py', 'planned', ['contracts', 'workflow']),
    ('tests/tools/test_local_validation.py', 'planned', ['contracts', 'workflow']),
    ('apps/records/models.py', 'blocked', []),
    ('unknown-input.bin', 'blocked', []),
])
def test_plan_selects_checks_from_changed_inputs(repo, name, mode, groups):
    before = git(repo, 'rev-parse', 'HEAD')
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(target.read_text() + '\n# changed\n' if target.exists() else 'changed')
    after = commit(repo)
    plan = validation.select_validation_plan(repo, before, after)
    assert plan['mode'] == mode
    assert plan['groups'] == groups
    assert plan['changed_paths'] == [name]
    assert plan['reason']
    assert plan['base_revision'] == before
    assert plan['revision'] == after


@pytest.mark.parametrize('name,mode', [('README.md', 'docs')])
def test_plan_lists_all_required_runner_groups(repo, name, mode):
    before = git(repo, 'rev-parse', 'HEAD')
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('changed')
    after = commit(repo)
    assert validation.select_validation_plan(repo, before, after)['groups'] == local_runner.required_steps(mode)


@pytest.mark.parametrize('change', ['mixed', 'delete', 'rename', 'mode', 'unknown', 'empty', 'unrelated'])
def test_plan_cannot_hide_unsafe_changes_behind_workflow_file(repo, change):
    script = repo / 'tools/submit.py'
    script.write_text('before')
    before = commit(repo)
    script.write_text('after')
    if change == 'mixed':
        (repo / 'apps').mkdir()
        (repo / 'apps/models.py').write_text('business')
    elif change == 'delete':
        script.unlink()
    elif change == 'rename':
        git(repo, 'mv', 'tools/submit.py', 'tools/submit_validation.py')
    elif change == 'mode':
        git(repo, 'add', '.')
        git(repo, 'update-index', '--chmod=+x', 'tools/submit.py')
        git(repo, 'commit', '-qm', 'mode')
    elif change == 'unknown':
        (repo / 'unknown.bin').write_text('input')
    elif change == 'unrelated':
        git(repo, 'checkout', '--orphan', 'unrelated')
    after = before if change == 'empty' else (git(repo, 'rev-parse', 'HEAD') if change == 'mode' else commit(repo))
    plan = validation.select_validation_plan(repo, before, after)
    assert plan['mode'] == 'blocked'
    assert plan['reason']


def seed_tests(repo, *paths):
    for name in paths:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('def test_synthetic(): pass\n' if name.endswith('.py') else "import test from 'node:test'; test('synthetic', () => {});\n")
    commit(repo)


def changed_plan(repo, *paths):
    before = git(repo, 'rev-parse', 'HEAD')
    for name in paths:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((target.read_text() if target.exists() else '') + '\n# changed\n')
    after = commit(repo)
    return validation.select_validation_plan(repo, before, after)


def test_static_prototype_screenshots_are_document_only(repo):
    plan = changed_plan(repo, 'prototype-gallery/screenshots/review.png')
    assert plan['mode'] == 'docs'
    assert plan['targets'] == {'python': [], 'browser': [], 'postgres': [], 'javascript': []}
    assert plan['full_recommended'] is False


def test_prototype_behavior_selects_its_own_tests(repo):
    seed_tests(repo, 'prototype-gallery/tests/test_lab_report_review_prototype.py',
               'prototype-gallery/tests/gallery.test.mjs')
    plan = changed_plan(repo, 'prototype-gallery/lab-report-review/app.js')
    assert plan['mode'] == 'planned'
    assert plan['groups'] == ['python']
    assert plan['targets'] == {'python': ['prototype-gallery/tests/test_lab_report_review_prototype.py'],
                               'browser': [], 'postgres': [], 'javascript': []}
    assert plan['full_recommended'] is False


def test_module_change_adds_direct_cross_directory_callers(repo):
    seed_tests(repo, 'tests/labs/test_models.py', 'tests/labs/test_dictionary.py',
               'tests/exports/test_lab_snapshot.py', 'tests/browser/test_lab_report_browser.py',
               'tests/integration/test_lab_report_postgres.py', 'tests/exports/test_other.py')
    (repo / 'tests/exports/test_lab_snapshot.py').write_text('from apps.labs.models import LabObservation\n')
    (repo / 'tests/browser/test_lab_report_browser.py').write_text('from apps.labs.views import report\n')
    (repo / 'tests/integration/test_lab_report_postgres.py').write_text('from apps.labs.models import LabObservation\n')
    commit(repo)
    plan = changed_plan(repo, 'apps/labs/models.py')
    assert plan['mode'] == 'planned'
    assert plan['groups'] == ['django', 'python', 'browser', 'postgres']
    assert plan['targets'] == {
        'python': ['tests/exports/test_lab_snapshot.py', 'tests/labs/test_dictionary.py', 'tests/labs/test_models.py'],
        'browser': ['tests/browser/test_lab_report_browser.py'],
        'postgres': ['tests/integration/test_lab_report_postgres.py'], 'javascript': []}
    assert plan['full_recommended'] is False


def test_multiple_modules_combine_related_targets_without_full(repo):
    seed_tests(repo, 'tests/labs/test_models.py', 'tests/exports/test_snapshot.py',
               'tests/glucose/test_views.py')
    (repo / 'tests/exports/test_snapshot.py').write_text('from apps.labs.models import LabObservation\nfrom apps.glucose.models import GlucoseEntry\n')
    commit(repo)
    plan = changed_plan(repo, 'apps/labs/models.py', 'apps/glucose/views.py')
    assert plan['mode'] == 'planned'
    assert plan['targets']['python'] == ['tests/exports/test_snapshot.py', 'tests/glucose/test_views.py', 'tests/labs/test_models.py']
    assert plan['full_recommended'] is False


def test_module_browser_route_is_covered_without_python_import(repo):
    seed_tests(repo, 'tests/labs/test_models.py', 'tests/browser/test_lab_report_consolidation_browser.py')
    (repo / 'tests/browser/test_lab_report_consolidation_browser.py').write_text(
        'def test_page(page): page.goto("/labs/compare/")\n')
    commit(repo)
    plan = changed_plan(repo, 'apps/labs/views.py')
    assert plan['targets']['browser'] == ['tests/browser/test_lab_report_consolidation_browser.py']


def test_module_route_alias_selects_cross_module_browser_test(repo):
    seed_tests(repo, 'tests/documents/test_views.py', 'tests/browser/test_family_sharing_browser.py')
    (repo / 'apps/documents').mkdir(parents=True)
    (repo / 'apps/documents/urls.py').write_text('path("records/<uuid:id>/", views.document_summary)\n')
    (repo / 'tests/browser/test_family_sharing_browser.py').write_text(
        'def test_record(page): page.goto("/records/123/")\n')
    commit(repo)
    plan = changed_plan(repo, 'apps/documents/views.py')
    assert plan['targets']['browser'] == ['tests/browser/test_family_sharing_browser.py']


def test_config_has_related_targets_and_recommends_full(repo):
    seed_tests(repo, 'tests/test_project_configuration.py', 'tests/deploy/test_release_artifacts.py')
    plan = changed_plan(repo, 'config/settings/base.py')
    assert plan['mode'] == 'planned'
    assert plan['targets']['python'] == ['tests/deploy/test_release_artifacts.py', 'tests/test_project_configuration.py']
    assert plan['full_recommended'] is True
    assert plan['risks']


def test_unknown_or_untested_module_is_blocked(repo):
    plan = changed_plan(repo, 'apps/unknown/views.py')
    assert plan['mode'] == 'blocked'
    assert 'apps/unknown/views.py' in plan['reason']


def test_local_frontend_asset_runs_its_page_and_js_checks(repo):
    seed_tests(repo, 'tests/browser/test_lab_report_browser.py',
               'tests/js/lab-report-workspace.test.mjs',
               'tests/browser/test_family_browser.py')
    (repo / 'tests/js/lab-report-workspace.test.mjs').write_text('const source = "static/js/lab-report-workspace.js";\n')
    commit(repo)
    plan = changed_plan(repo, 'static/js/lab-report-workspace.js')
    assert plan['mode'] == 'planned'
    assert plan['groups'] == ['browser', 'javascript']
    assert plan['targets']['browser'] == ['tests/browser/test_lab_report_browser.py']
    assert plan['targets']['javascript'] == ['tests/js/lab-report-workspace.test.mjs']
    assert plan['full_recommended'] is False


def test_shared_styles_expand_to_shared_ui_and_recommend_full(repo):
    seed_tests(repo, 'tests/ui/test_design_tokens.py', 'tests/accessibility/test_shell_markup.py',
               'tests/browser/test_shell_browser.py')
    plan = changed_plan(repo, 'static/css/tokens.css')
    assert plan['mode'] == 'planned'
    assert plan['targets']['python'] == ['tests/accessibility/test_shell_markup.py', 'tests/ui/test_design_tokens.py']
    assert plan['targets']['browser'] == ['tests/browser/test_shell_browser.py']
    assert plan['full_recommended'] is True


def test_migration_selects_module_and_database_tests_with_full_advice(repo):
    seed_tests(repo, 'tests/labs/test_models.py', 'tests/integration/test_lab_report_postgres.py')
    (repo / 'tests/integration/test_lab_report_postgres.py').write_text('from apps.labs.models import LabObservation\n')
    commit(repo)
    plan = changed_plan(repo, 'apps/labs/migrations/0014_new_field.py')
    assert plan['mode'] == 'planned'
    assert plan['groups'] == ['django', 'python', 'postgres']
    assert plan['targets']['postgres'] == ['tests/integration/test_lab_report_postgres.py']
    assert plan['full_recommended'] is True


def test_runtime_prototype_asset_runs_prototype_checks(repo):
    seed_tests(repo, 'prototype-gallery/lab-report-review/tests/test_lab_report_review_prototype.py')
    plan = changed_plan(repo, 'prototype-gallery/lab-report-review/assets/report-page.svg')
    assert plan['mode'] == 'planned'
    assert plan['targets']['python'] == ['prototype-gallery/lab-report-review/tests/test_lab_report_review_prototype.py']


def test_shared_test_fixture_selects_related_tool_checks_and_recommends_full(repo):
    seed_tests(repo, 'tests/test_project_configuration.py', 'tests/tools/test_required_tests.py')
    plan = changed_plan(repo, 'tests/conftest.py')
    assert plan['mode'] == 'planned'
    assert plan['targets']['python'] == ['tests/test_project_configuration.py', 'tests/tools/test_required_tests.py']
    assert plan['full_recommended'] is True


def test_fixture_subdirectory_runs_its_own_module_tests(repo):
    seed_tests(repo, 'tests/facts/test_synthetic_evaluation.py', 'tests/labs/test_models.py',
               'tests/test_project_configuration.py')
    (repo / 'tests/facts/test_synthetic_evaluation.py').write_text(
        'def test_input(): path = "tests/fixtures/facts/synthetic-corpus.json"\n')
    commit(repo)
    plan = changed_plan(repo, 'tests/fixtures/facts/synthetic-corpus.json')
    assert plan['mode'] == 'planned'
    assert 'tests/facts/test_synthetic_evaluation.py' in plan['targets']['python']
    assert 'tests/labs/test_models.py' not in plan['targets']['python']


def test_required_test_runner_selects_its_own_test_and_recommends_full(repo):
    seed_tests(repo, 'tests/tools/test_required_tests.py')
    plan = changed_plan(repo, 'tools/run_required_tests.py')
    assert plan['mode'] == 'planned'
    assert plan['targets']['python'] == ['tests/tools/test_required_tests.py']
    assert plan['full_recommended'] is True


def test_module_level_postgres_marker_runs_only_database_group(repo):
    seed_tests(repo, 'tests/integration/test_notification_account_deletion_lock_order.py')
    (repo / 'tests/integration/test_notification_account_deletion_lock_order.py').write_text(
        'import pytest\npytestmark = [pytest.mark.django_db, pytest.mark.postgres]\ndef test_lock_order(): pass\n')
    commit(repo)
    plan = changed_plan(repo, 'tests/integration/test_notification_account_deletion_lock_order.py')
    assert plan['targets']['postgres'] == ['tests/integration/test_notification_account_deletion_lock_order.py']
    assert plan['targets']['python'] == []


def test_mixed_postgres_markers_run_both_database_and_python(repo):
    seed_tests(repo, 'tests/cloud_imaging/test_error_form_freshness.py')
    (repo / 'tests/cloud_imaging/test_error_form_freshness.py').write_text(
        'import pytest\ndef test_regular(): pass\n@pytest.mark.postgres\ndef test_database(): pass\n')
    commit(repo)
    plan = changed_plan(repo, 'tests/cloud_imaging/test_error_form_freshness.py')
    assert plan['targets']['postgres'] == ['tests/cloud_imaging/test_error_form_freshness.py']
    assert plan['targets']['python'] == ['tests/cloud_imaging/test_error_form_freshness.py']


def test_example_environment_uses_config_checks_without_full_advice(repo):
    seed_tests(repo, 'tests/test_project_configuration.py')
    plan = changed_plan(repo, '.env.example')
    assert plan['mode'] == 'planned'
    assert plan['targets']['python'] == ['tests/test_project_configuration.py']
    assert plan['full_recommended'] is False


def test_playwright_config_uses_browser_checks_without_business_full_advice(repo):
    seed_tests(repo, 'tests/browser/test_shell_browser.py')
    plan = changed_plan(repo, 'playwright.config.ts')
    assert plan['mode'] == 'planned'
    assert plan['targets']['browser'] == ['tests/browser/test_shell_browser.py']
    assert plan['full_recommended'] is False


@pytest.mark.parametrize('helper,test_file,group', [
    ('tests/browser/sqlite_server.py', 'tests/browser/test_lab_report_browser.py', 'browser'),
    ('tests/documents/fakes.py', 'tests/documents/test_views.py', 'python'),
    ('tests/treatments/factories.py', 'tests/treatments/test_services.py', 'python'),
])
def test_test_helper_runs_own_directory_suite(repo, helper, test_file, group):
    seed_tests(repo, test_file)
    plan = changed_plan(repo, helper)
    assert plan['mode'] == 'planned'
    assert plan['targets'][group] == [test_file]


def test_planned_business_validation_seals_exact_targets(repo, tmp_path):
    seed_tests(repo, 'tests/labs/test_models.py', 'tests/exports/test_snapshot.py')
    (repo / 'tests/exports/test_snapshot.py').write_text('from apps.labs.models import LabObservation\n')
    before = commit(repo)
    (repo / 'apps/labs').mkdir(parents=True)
    (repo / 'apps/labs/models.py').write_text('changed')
    after = commit(repo)
    receipt = validation.validate_revision(repo, after, tmp_path / 'state', mode='planned', base_revision=before)
    plan = validation.select_validation_plan(repo, before, after)
    assert receipt['plan'] == plan
    assert receipt['coverage_targets'] == plan['targets']
    assert receipt['full_recommended'] is False
    assert receipt['risks'] == []


def test_full_recommendation_requires_explicit_scope_decision(repo, tmp_path):
    seed_tests(repo, 'tests/test_project_configuration.py')
    before = git(repo, 'rev-parse', 'HEAD')
    (repo / 'config/settings').mkdir(parents=True)
    (repo / 'config/settings/base.py').write_text('changed')
    after = commit(repo)
    plan = validation.select_validation_plan(repo, before, after)
    with pytest.raises(validation.ValidationError, match='scope decision'):
        validation.validate_revision(repo, after, tmp_path / 'state', mode='planned', base_revision=before)
    receipt = validation.validate_revision(repo, after, tmp_path / 'state', mode='planned',
                                           base_revision=before, plan_digest=validation._digest(plan),
                                           scope_decision='skip')
    assert receipt['scope_decision'] == 'skip'
    assert receipt['full_recommended'] is True
    assert receipt['risks'] == plan['risks']


def test_release_fallback_checks_business_plan_on_actual_release_candidate(repo, tmp_path):
    seed_tests(repo, 'tests/labs/test_models.py')
    before = git(repo, 'rev-parse', 'HEAD')
    (repo / 'apps/labs').mkdir(parents=True)
    (repo / 'apps/labs/models.py').write_text('changed')
    feature = commit(repo)
    candidate = release(repo)
    receipt = validation.validate_revision(repo, candidate, tmp_path / 'state', mode='planned',
                                           base_revision=before, plan_revision=feature)
    assert receipt['revision'] == candidate
    assert receipt['plan_source_revision'] == feature
    assert receipt['coverage_targets']['python'] == ['tests/labs/test_models.py']
    released = validation.validate_revision(repo, candidate, tmp_path / 'state', mode='release',
                                            baseline_receipt=receipt['receipt_path'])
    assert released['baseline_mode'] == 'planned'
    assert released['coverage_targets'] == receipt['coverage_targets']


def planned_receipt(repo, state):
    before = git(repo, 'rev-parse', 'HEAD')
    (repo / 'tools/submit.py').write_text('changed submit')
    (repo / 'README.md').write_text('workflow instructions')
    revision = commit(repo)
    return validation.validate_revision(repo, revision, state, mode='planned', base_revision=before)


def test_planned_validation_proves_scope_and_retains_actual_steps(repo, tmp_path):
    first = planned_receipt(repo, tmp_path / 'state')
    assert first['mode'] == 'planned'
    assert first['business_reused'] is False
    assert first['plan'] == validation.select_validation_plan(repo, first['base_revision'], first['revision'])
    assert first['plan_digest'] == validation._digest(first['plan'])
    result = json.loads(Path(first['result_path']).read_text())
    assert [step['name'] for step in result['steps']] == first['plan']['groups']
    second = validation.validate_revision(repo, first['revision'], tmp_path / 'state', mode='planned', base_revision=first['base_revision'])
    assert second['reused'] is True


@pytest.mark.parametrize('base', [None, 'HEAD', 'absent'])
def test_planned_validation_rejects_unproven_scope(repo, tmp_path, base):
    with pytest.raises(validation.ValidationError, match='Planned validation requires'):
        validation.validate_revision(repo, 'HEAD', tmp_path / 'state', mode='planned', base_revision=base)


def test_planned_validation_rejects_business_changes(repo, tmp_path):
    before = git(repo, 'rev-parse', 'HEAD')
    (repo / 'apps').mkdir()
    (repo / 'apps/models.py').write_text('business')
    (repo / 'tools/submit.py').write_text('workflow')
    after = commit(repo)
    with pytest.raises(validation.ValidationError, match='Planned validation requires'):
        validation.validate_revision(repo, after, tmp_path / 'state', mode='planned', base_revision=before)


def test_release_reuses_proven_plan_without_claiming_business_tests(repo, tmp_path):
    first = planned_receipt(repo, tmp_path / 'state')
    candidate = release(repo)
    receipt = validation.validate_revision(repo, candidate, tmp_path / 'state', mode='release', baseline_receipt=first['receipt_path'])
    assert receipt['baseline_mode'] == 'planned'
    assert receipt['validation_reused'] is True
    assert receipt['business_reused'] is False
    assert receipt['baseline_digest'] == first['receipt_digest']


@pytest.mark.parametrize('change', ['plan', 'base', 'tree', 'environment', 'command', 'source', 'artifact'])
def test_release_rejects_unproven_planned_receipt(repo, tmp_path, monkeypatch, change):
    state = tmp_path / 'state'
    first = planned_receipt(repo, state)
    if change == 'environment':
        monkeypatch.setenv('LANG', 'different')
    elif change == 'source':
        (repo / 'tools/submit.py').write_text('unvalidated workflow')
        commit(repo)
    elif change == 'artifact':
        Path(first['result_path']).with_name('browser.xml').write_text('changed')
    else:
        if change == 'plan':
            first['plan']['groups'] = ['contracts']
            first['plan_digest'] = validation._digest(first['plan'])
        elif change == 'base':
            first['base_revision'] = first['revision']
        elif change == 'tree':
            first['tree'] = 'bad-tree'
        elif change == 'command':
            first['command_digest'] = 'different'
        first.pop('receipt_digest')
        first['receipt_digest'] = validation._digest(first)
        Path(first['receipt_path']).write_text(json.dumps(first))
        for path in (state / 'cache').glob('*.json'):
            path.unlink()
    candidate = release(repo)
    with pytest.raises(validation.ReuseUnavailable):
        validation.validate_revision(repo, candidate, state, mode='release', baseline_receipt=first['receipt_path'])


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


def test_snapshot_preserves_blob_bytes_with_windows_line_ending_configuration(repo, tmp_path):
    (repo / 'app.py').write_bytes(b'committed LF source\n')
    revision = commit(repo)
    git(repo, 'config', 'core.autocrlf', 'true')
    git(repo, 'config', 'core.eol', 'crlf')
    result = validation.validate_revision(repo, revision, tmp_path / 'state')
    artifact = json.loads(Path(result['result_path']).read_text())
    assert artifact['files']['app.py'] == 'committed LF source\n'
    assert git(repo, 'config', 'core.autocrlf') == 'true'
    assert git(repo, 'config', 'core.eol') == 'crlf'


@pytest.mark.parametrize('name,expected', [
    ('README.md', 'docs'), ('AGENTS.md', 'docs'), ('CHANGELOG.md', 'docs'),
    ('docs/README.md', 'docs'), ('docs/specs/example.md', 'docs'),
    ('docs/document-registry.json', 'docs'),
    ('docs/verification/artifacts/result.json', 'docs'),
    ('docs/verification/artifacts/result.xml', 'docs'),
    ('docs/verification/traceability.json', 'docs'),
    ('docs/verification/release-evidence.json', 'docs'),
    ('docs/verification/attestations/review.json', 'docs'),
    ('.github/pull_request_template.md', 'docs'),
    ('.github/PULL_REQUEST_TEMPLATE/feature.md', 'docs'),
    ('.github/ISSUE_TEMPLATE/nested/bug.md', 'docs'),
    ('app.py', 'full'), ('tools/check.py', 'full'), ('docs/run.py', 'full'),
    ('docs/data.json', 'full'), ('docs/verification/artifacts/run.sh', 'full'),
    ('docs/verification/artifacts/phase-two-baseline-manifest.json', 'full'),
    ('docs/verification/artifacts/labs-extraction-scope-baseline-manifest.json', 'full'),
    ('docs/verification/artifacts/phase-two-annotation-adjudication-report.json', 'full'),
    ('docs/licenses/noto-sans-sc-ofl.txt', 'full'),
    ('docs/licenses/noto-sans-sc-provenance.json', 'full'),
    ('docs/deployment/local/tencent-cloud/README.md', 'full'),
    ('.github/workflows/diagnostic.yml', 'full'), ('.github/unknown.md', 'full'),
])
def test_validation_scope_uses_complete_committed_diff(repo, name, expected):
    before = git(repo, 'rev-parse', 'HEAD')
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('changed')
    after = commit(repo)
    assert validation.select_validation_mode(repo, before, after) == expected


@pytest.mark.parametrize('change', ['mixed', 'code-to-doc', 'doc-to-code', 'executable', 'symlink', 'empty', 'unrelated'])
def test_docs_scope_does_not_hide_business_or_mode_changes(repo, change):
    (repo / 'README.md').write_text('docs')
    before = commit(repo)
    (repo / 'README.md').write_text('updated docs')
    if change == 'mixed':
        (repo / 'app.py').write_text('changed code')
    elif change == 'code-to-doc':
        (repo / 'docs').mkdir()
        git(repo, 'mv', 'app.py', 'docs/example.md')
    elif change == 'doc-to-code':
        git(repo, 'mv', 'README.md', 'guide.py')
    elif change in ('executable', 'symlink'):
        mode = '100755' if change == 'executable' else '120000'
        blob = git(repo, 'hash-object', '-w', 'README.md')
        git(repo, 'update-index', '--cacheinfo', f'{mode},{blob},README.md')
        git(repo, 'commit', '-qm', 'mode change')
        after = git(repo, 'rev-parse', 'HEAD')
    elif change == 'empty':
        after = before
    elif change == 'unrelated':
        git(repo, 'checkout', '--orphan', 'unrelated')
        after = commit(repo)
    if change in ('mixed', 'code-to-doc', 'doc-to-code'):
        after = commit(repo)
    assert validation.select_validation_mode(repo, before, after) == 'full'


def test_docs_mode_allows_regular_document_deletion(repo, tmp_path):
    (repo / 'README.md').write_text('obsolete')
    before = commit(repo)
    (repo / 'README.md').unlink()
    after = commit(repo)
    result = validation.validate_revision(repo, after, tmp_path / 'state', mode='docs', base_revision=before)
    assert result['mode'] == 'docs'
    assert result['base_revision'] == before
    assert result['business_reused'] is False
    artifact = json.loads(Path(result['result_path']).read_text())
    assert [step['name'] for step in artifact['steps']] == ['documentation', 'traceability', 'release-gate', 'version']


@pytest.mark.parametrize('base', [None, 'HEAD', 'HEAD~1'])
def test_docs_mode_cannot_skip_proving_document_only_diff(repo, tmp_path, base):
    (repo / 'app.py').write_text('changed code')
    commit(repo)
    with pytest.raises(validation.ValidationError, match='document'):
        validation.validate_revision(repo, 'HEAD', tmp_path / 'state', mode='docs', base_revision=base)
    assert not list((tmp_path / 'state').rglob('result.json'))


def test_docs_receipt_never_satisfies_full_validation(repo, tmp_path):
    before = git(repo, 'rev-parse', 'HEAD')
    (repo / 'README.md').write_text('docs')
    after = commit(repo)
    state = tmp_path / 'state'
    docs = validation.validate_revision(repo, after, state, mode='docs', base_revision=before)
    full = validation.validate_revision(repo, after, state)
    assert docs['cache_key'] != full['cache_key']
    assert full['mode'] == 'full' and full['reused'] is False
    artifact = json.loads(Path(full['result_path']).read_text())
    assert [step['name'] for step in artifact['steps']] == ['business', 'docker']


def test_full_request_reuses_business_after_only_documentation_changes(repo, tmp_path):
    state = tmp_path / 'state'
    full = validation.validate_revision(repo, 'HEAD', state)
    (repo / 'README.md').write_text('follow-up documentation')
    revision = commit(repo)
    result = validation.validate_revision(repo, revision, state)
    assert result['mode'] == 'docs'
    assert result['business_reused'] is True
    assert result['baseline_revision'] == full['revision']
    assert result['baseline_digest'] == full['receipt_digest']
    assert result['revision'] == revision
    artifacts = [json.loads(path.read_text()) for path in state.rglob('result.json')]
    assert len(artifacts) == 2
    assert sum(item['mode'] == 'full' for item in artifacts) == 1
    docs_result = json.loads(Path(result['result_path']).read_text())
    assert [step['name'] for step in docs_result['steps']] == ['documentation', 'traceability', 'release-gate', 'version']
    assert result['environment']['environment'] == 'C-docs'
    standalone = validation.validate_revision(repo, revision, state, mode='docs', base_revision=full['revision'])
    assert standalone['business_reused'] is False


@pytest.mark.parametrize('change', ['code', 'environment', 'evidence', 'unrelated'])
def test_full_request_does_not_reuse_unproven_business_results(repo, tmp_path, monkeypatch, change):
    state = tmp_path / 'state'
    full = validation.validate_revision(repo, 'HEAD', state)
    (repo / 'README.md').write_text('follow-up documentation')
    if change == 'code':
        (repo / 'app.py').write_text('new business code')
    elif change == 'environment':
        monkeypatch.setenv('LANG', 'C.UTF-8')
    elif change == 'evidence':
        (Path(full['result_path']).parent / 'browser.xml').unlink()
    elif change == 'unrelated':
        git(repo, 'checkout', '--orphan', 'unrelated')
    revision = commit(repo)
    result = validation.validate_revision(repo, revision, state)
    assert result['mode'] == 'full'
    assert result['business_reused'] is False
    assert result['reused'] is False


def test_reused_business_does_not_hide_failed_documentation_checks(repo, tmp_path):
    state = tmp_path / 'state'
    validation.validate_revision(repo, 'HEAD', state)
    (repo / 'README.md').write_text('follow-up documentation')
    revision = commit(repo)
    (tmp_path / 'runner-status').write_text('failed')
    with pytest.raises(validation.ValidationError) as caught:
        validation.validate_revision(repo, revision, state)
    assert type(caught.value) is validation.ValidationError
    modes = [json.loads(path.read_text())['mode'] for path in state.rglob('result.json')]
    assert sorted(modes) == ['docs', 'full']


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


@pytest.mark.parametrize('supplied', ['full', 'docs', 'missing', 'stale', 'corrupt-receipt'])
def test_release_finds_intact_full_proof_across_document_commits(repo, tmp_path, supplied):
    state = tmp_path / 'state'
    full = validation.validate_revision(repo, 'HEAD', state)
    (repo / 'README.md').write_text('intervening docs')
    docs_revision = commit(repo)
    docs = validation.validate_revision(repo, docs_revision, state, mode='docs', base_revision=full['revision'])
    path = full['receipt_path']
    if supplied == 'docs':
        path = docs['receipt_path']
    elif supplied == 'missing':
        path = None
    elif supplied == 'stale':
        receipt = json.loads(Path(path).read_text())
        receipt['policy_digest'] = 'obsolete-policy'
        receipt.pop('receipt_digest')
        receipt['receipt_digest'] = validation._digest(receipt)
        Path(path).write_text(json.dumps(receipt))
    elif supplied == 'corrupt-receipt':
        Path(path).write_text('{}')
    revision = release(repo)
    result = validation.validate_revision(repo, revision, state, mode='release', baseline_receipt=path)
    assert result['business_reused'] is True
    assert result['baseline_revision'] == full['revision']
    assert result['baseline_digest'] == full['receipt_digest']
    assert result['mode'] == 'release'
    assert len(list(state.rglob('result.json'))) == 3


@pytest.mark.parametrize('change', ['code', 'manifest', 'unknown', 'rename', 'mode', 'dependency'])
def test_release_cache_search_preserves_source_and_metadata_boundaries(repo, tmp_path, change):
    state = tmp_path / 'state'
    validation.validate_revision(repo, 'HEAD', state)
    (repo / 'README.md').write_text('safe documentation')
    if change == 'code':
        (repo / 'app.py').write_text('new business logic')
    elif change == 'manifest':
        path = repo / 'docs/verification/artifacts/phase-two-baseline-manifest.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
    elif change == 'unknown':
        (repo / 'docs').mkdir()
        (repo / 'docs/input.json').write_text('{}')
    elif change == 'rename':
        (repo / 'docs').mkdir()
        git(repo, 'mv', 'app.py', 'docs/code.md')
    commit(repo)
    release(repo)
    if change == 'mode':
        git(repo, 'update-index', '--chmod=+x', 'README.md')
        git(repo, 'commit', '-qm', 'executable doc')
    elif change == 'dependency':
        path = repo / 'pyproject.toml'
        path.write_text(path.read_text().replace('example==1', 'example==2'))
        commit(repo)
    with pytest.raises(validation.ReuseUnavailable):
        validation.validate_revision(repo, 'HEAD', state, mode='release')


def test_release_cannot_use_only_docs_proof(repo, tmp_path):
    before = git(repo, 'rev-parse', 'HEAD')
    (repo / 'README.md').write_text('docs')
    after = commit(repo)
    state = tmp_path / 'state'
    docs = validation.validate_revision(repo, after, state, mode='docs', base_revision=before)
    release(repo)
    with pytest.raises(validation.ReuseUnavailable):
        validation.validate_revision(repo, 'HEAD', state, mode='release', baseline_receipt=docs['receipt_path'])


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
    for cached in (state / 'cache').glob('*.json'):
        cached.unlink()
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
        for cached in (state / 'cache').glob('*.json'):
            cached.unlink()
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


def test_runner_process_failure_reports_evidence_directory_without_stdout(repo, tmp_path):
    state = tmp_path / 'state'
    (tmp_path / 'runner-status').write_text('process-error')
    with pytest.raises(validation.ValidationError) as caught:
        validation.validate_revision(repo, 'HEAD', state)
    results = list(state.rglob('result.json'))
    assert len(results) == 1
    assert str(results[0].parent.resolve()) in str(caught.value)
    assert 'synthetic-hidden-stdout' not in str(caught.value)


@pytest.mark.skipif(os.name != 'nt', reason='Windows WSL argument boundary')
def test_windows_runner_uses_direct_wsl_arguments_for_paths_with_spaces(tmp_path, monkeypatch):
    runner = tmp_path / 'source with spaces' / 'local_validation.py'
    archive = tmp_path / 'source with spaces' / 'source.tar'
    calls = []
    def wslpath(command, **kwargs):
        calls.append(command)
        return ('/mnt/c/source with spaces/' + Path(command[-1]).name + '\n').encode()
    monkeypatch.setattr(validation, '_run', wslpath)
    command = validation._runner_command(runner, ['--fingerprint', '--archive', archive])
    assert all(call[:6] == ['wsl.exe', '-d', 'Ubuntu-24.04', '--exec', 'wslpath', '-a'] for call in calls)
    assert command == ['wsl.exe', '-d', 'Ubuntu-24.04', '--exec', 'python3',
                       '/mnt/c/source with spaces/local_validation.py', '--fingerprint',
                       '--archive', '/mnt/c/source with spaces/source.tar']
