from copy import copy, deepcopy
from dataclasses import replace
import uuid

import pytest

from apps.documents.models import ProcessingRun
from apps.labs.models import LabReportRevision
from apps.labs.reports import _from_snapshot, correct_report, effective_report, persist_report_units
from apps.processing.models import ParsingVersion
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db
SOURCE = {'page_number': 1, 'polygon': [[.1, .1], [.9, .1], [.9, .2], [.1, .2]]}


def next_report_version(unit, identities=None):
    previous = unit.parsing_version
    run = ProcessingRun.objects.create(document=previous.document, parser_version='report-reparse',
        task_type='reparse', idempotency_key=str(uuid.uuid4()), attempt_number=previous.processing_run.attempt_number + 1)
    version = ParsingVersion.objects.create(document=previous.document, processing_run=run, previous_version=previous,
        parser_version=run.parser_version, ocr_provider='fixture', ocr_provider_version='1', status='READY',
        dictionary_version=previous.dictionary_version, dictionary_hash=previous.dictionary_hash)
    for old in previous.lab_observations.select_related('evidence'):
        evidence = copy(old.evidence)
        evidence.pk, evidence.parsing_version = uuid.uuid4(), version
        evidence.save(force_insert=True)
        row = copy(old)
        row.pk, row.parsing_version, row.evidence, row.report_unit = uuid.uuid4(), version, evidence, None
        row.revision_number = 0
        row.save(force_insert=True)
    units = persist_report_units(version, identities or (_from_snapshot(unit.automatic),))
    ParsingVersion.objects.activate(version)
    for item in units:
        item.refresh_from_db()
    return units


@pytest.mark.parametrize('field,value', [('institution', '核对后的医院'), ('report_number', 'CORRECTED'),
                                       ('sampled_at', '2026-09-17 10:30')])
def test_identical_reparse_keeps_report_correction_and_automatic_evidence(django_user_model, field, value):
    client, patient = _patient(django_user_model, 'report-inheritance-' + field)
    _, _, original = report(patient)
    automatic = deepcopy(original.automatic)
    correct_report(patient, patient.account, original.pk, {field: value}, expected_revision=0,
                   source_evidence=SOURCE, rationale='原件核对记录', operation_id='original-correction')
    current, = next_report_version(original)
    identity = effective_report(current)
    assert (identity.sampling_label if field == 'sampled_at' else getattr(identity, field)) == value
    assert identity.status == 'ACCEPTED'
    original.refresh_from_db()
    assert original.automatic == current.automatic == automatic
    assert LabReportRevision.objects.count() == 1
    response = client.get(f'/labs/reports/{current.pk}/')
    assert response.status_code == 200
    assert '原件核对记录' in response.content.decode()


def test_changed_report_evidence_retains_correction_but_requires_review(django_user_model):
    from apps.exports.content import build_snapshot
    from apps.labs.trends import trend_view
    from apps.labs.readmodels import effective_rows
    from apps.labs.validation import validate_observation

    _, patient = _patient(django_user_model, 'report-inheritance-conflict')
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '核对后的医院'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='原件医院', operation_id='hospital')
    changed = replace(_from_snapshot(original.automatic), report_number='CHANGED')
    current, = next_report_version(original, (changed,))
    identity = effective_report(current)
    assert identity.institution == '核对后的医院'
    assert identity.report_number == 'CHANGED'
    assert identity.status == 'REVIEW'
    assert identity.reason == 'report_revision_conflict'
    visible, = effective_rows(patient)
    issues = validate_observation(visible)
    assert any(item['code'] == 'report_identity_conflict' for item in issues)
    trend = trend_view(patient, 'LAB_WBC', include_history=True)
    assert not trend.series and len(trend.disputed) == 1
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert snapshot['labs'][0]['report']['status'] == 'REVIEW'
    assert snapshot['lab_results'][0]['disputed']


def test_report_revision_does_not_follow_an_ordinal_to_another_region(django_user_model):
    client, patient = _patient(django_user_model, 'report-inheritance-region')
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '不可挪用的医院'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='旧报告位置核对', operation_id='located')
    moved = replace(_from_snapshot(original.automatic), source_region=((0, .7), (1, .7), (1, 1), (0, 1)))
    current, = next_report_version(original, (moved,))
    assert effective_report(current).institution == original.automatic['institution']
    assert '旧报告位置核对' in client.get(f'/labs/reports/{current.pk}/').content.decode()


@pytest.mark.parametrize('decision', ['KEEP_REVISION', 'USE_AUTOMATIC'])
def test_reparse_conflict_can_be_resolved_with_source_fencing_and_audit(django_user_model, decision):
    client, patient = _patient(django_user_model, 'report-reconcile-' + decision)
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '核对后的医院'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='原件医院', operation_id='hospital')
    changed = replace(_from_snapshot(original.automatic), report_number='CHANGED')
    current, = next_report_version(original, (changed,))
    path = f'/labs/reports/{current.pk}/'
    response = client.get(path)
    assert '本次识别值' in response.content.decode()
    payload = {'decision': decision, 'expected_revision': 0, 'expected_source': response.context['source_token'],
               'source_index': '0', 'rationale': '对照原件核对新旧依据', 'operation_id': 'reconcile'}
    assert client.post(path, payload).status_code == 302
    assert client.post(path, payload).status_code == 302
    current.refresh_from_db()
    effective = effective_report(current)
    assert effective.status == 'ACCEPTED'
    assert effective.institution == ('核对后的医院' if decision == 'KEEP_REVISION' else changed.institution)
    event = current.revisions.get()
    assert event.action == decision
    assert event.inherited_from_id == original.revisions.get().pk
    assert current.automatic['report_number'] == 'CHANGED'
    assert LabReportRevision.objects.count() == 2
    assert client.post(path, {**payload, 'decision': 'USE_AUTOMATIC' if decision == 'KEEP_REVISION' else 'KEEP_REVISION'}).status_code == 409


def test_old_report_page_cannot_overwrite_a_changed_inherited_revision(django_user_model):
    client, patient = _patient(django_user_model, 'report-inherited-stale')
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '第一次核对医院'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='原件医院', operation_id='first')
    current, = next_report_version(original)
    path = f'/labs/reports/{current.pk}/'
    response = client.get(path)
    payload = {'field': 'report_number', 'value': 'STALE', 'expected_revision': 0,
               'expected_source': response.context['source_token'], 'source_index': '0',
               'rationale': '旧页面提交', 'operation_id': 'stale'}
    ParsingVersion.objects.activate(original.parsing_version)
    correct_report(patient, patient.account, original.pk, {'institution': '第二次核对医院'}, expected_revision=1,
                   source_evidence=SOURCE, rationale='再次核对原件', operation_id='second')
    ParsingVersion.objects.activate(current.parsing_version)
    assert client.post(path, payload).status_code == 409
    current.refresh_from_db()
    assert current.revision_number == 0
    assert effective_report(current).institution == '第二次核对医院'


@pytest.mark.django_db(transaction=True)
def test_report_revision_migration_preserves_existing_correction(django_user_model):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    _, patient = _patient(django_user_model, 'report-revision-migration')
    _, _, unit = report(patient)
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    target = [('labs', '0005_labreportunit_labreportrevision_and_more')]
    try:
        executor.migrate(target)
        legacy = executor.loader.project_state(target).apps
        event = legacy.get_model('labs', 'LabReportRevision').objects.create(unit_id=unit.pk,
            author_id=patient.account_id, sequence=1, operation_id='legacy-report-correction',
            before=unit.automatic, after=unit.automatic, source_evidence=SOURCE, rationale='原有报告核对记录')
        expected = {'before': event.before, 'after': event.after, 'source_evidence': event.source_evidence,
                    'author_id': event.author_id, 'created_at': event.created_at, 'rationale': event.rationale}
        MigrationExecutor(connection).migrate(leaves)
        migrated = LabReportRevision.objects.get(pk=event.pk)
        assert migrated.action == 'CORRECT' and migrated.inherited_from_id is None
        assert {key: getattr(migrated, key) for key in expected} == expected
    finally:
        MigrationExecutor(connection).migrate(leaves)


def test_duplicate_report_regions_cannot_receive_an_inherited_correction(django_user_model):
    _, patient = _patient(django_user_model, 'report-inheritance-ambiguous')
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '不可自动套用的医院'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='原报告核对', operation_id='original')
    identity = _from_snapshot(original.automatic)
    units = next_report_version(original, (replace(identity, ordinal=1), replace(identity, ordinal=2)))
    assert all(effective_report(unit).institution == identity.institution for unit in units)
    assert LabReportRevision.objects.count() == 1
