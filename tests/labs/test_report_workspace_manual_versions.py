from copy import copy
from dataclasses import replace
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.documents.models import ProcessingRun
from apps.labs.models import LabObservation
from apps.labs.readmodels import effective_rows
from apps.labs.report_workspace import report_workspace, submit_report_workspace
from apps.labs.reports import _from_snapshot, persist_report_units
from apps.processing.models import ParsingVersion, SourceEvidence
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def _manual_report(patient):
    _, old, unit = report(patient)
    old.delete()
    current = report_workspace(patient)['current']
    submit_report_workspace(patient, patient.account, current['key'], current['token'], uuid.uuid4(),
        {'additions': [{'unit_id': str(unit.pk), 'raw_name': '合成目录外项目', 'raw_value': '7'}]}, confirm=True)
    return unit, LabObservation.objects.get(manual_identity__isnull=False)


def _reparse(unit, *, recognized_value=None, moved=False):
    previous = unit.parsing_version
    ProcessingRun.objects.filter(document=previous.document, is_current=True).update(is_current=False)
    run = ProcessingRun.objects.create(document=previous.document, parser_version='manual-reparse',
        task_type='reparse', idempotency_key=str(uuid.uuid4()),
        attempt_number=previous.processing_run.attempt_number + 1,
        stage='SUCCEEDED', finished_at=timezone.now())
    version = ParsingVersion.objects.create(document=previous.document, processing_run=run,
        previous_version=previous, parser_version=run.parser_version, ocr_provider='fixture',
        ocr_provider_version='1', status='READY', dictionary_version=previous.dictionary_version,
        dictionary_hash=previous.dictionary_hash, diagnostics=previous.diagnostics)
    if recognized_value is not None:
        source = SourceEvidence.objects.create(parsing_version=version, document_page=unit.document_page,
            source_text=f'合成目录外项目 {recognized_value}', confidence='.99',
            polygon=[[.1, .1], [.9, .1], [.9, .2], [.1, .2]])
        old = LabObservation.objects.get(manual_identity__isnull=False, parsing_version=previous)
        row = copy(old)
        row.pk, row.parsing_version, row.evidence, row.report_unit = uuid.uuid4(), version, source, None
        row.manual_identity = row.manual_created_by = None
        row.raw_value, row.revision_number = recognized_value, 0
        row.save(force_insert=True)
    identity = _from_snapshot(unit.automatic)
    if moved:
        identity = replace(identity, source_region=((0, .7), (1, .7), (1, 1), (0, 1)))
    current, = persist_report_units(version, (identity,))
    ParsingVersion.objects.activate(version)
    return current


def test_manual_observation_survives_reparse_that_still_misses_it(django_user_model):
    _, patient = _patient(django_user_model, 'manual-reparse-missing')
    unit, original = _manual_report(patient)
    current = _reparse(unit)
    rows = list(LabObservation.objects.filter(parsing_version=current.parsing_version))
    assert len(rows) == 1
    assert rows[0].manual_identity == original.manual_identity
    assert rows[0].evidence.origin == 'MANUAL' and rows[0].manual_created_by_id == patient.account_id
    assert rows[0].report_unit_id == current.pk
    assert len(effective_rows(patient)) == 1
    assert report_workspace(patient)['current']['confirmed'] is False
    assert LabObservation.objects.filter(manual_identity=original.manual_identity).count() == 2


def test_unique_recognition_links_manual_identity_without_duplicate_effective_row(django_user_model):
    _, patient = _patient(django_user_model, 'manual-reparse-unique')
    unit, original = _manual_report(patient)
    current = _reparse(unit, recognized_value='7')
    rows = list(LabObservation.objects.filter(parsing_version=current.parsing_version))
    assert len(rows) == 1 and rows[0].manual_identity == original.manual_identity
    assert rows[0].evidence.origin == 'AUTOMATIC'
    assert len(effective_rows(patient)) == 1
    assert not report_workspace(patient)['current']['problems']


@pytest.mark.parametrize('recognized_value,moved', [('8', False), ('7', True)])
def test_conflicting_value_or_report_region_is_pending_not_double_counted(django_user_model,
                                                                          recognized_value, moved):
    _, patient = _patient(django_user_model, 'manual-reparse-conflict' + str(moved))
    unit, original = _manual_report(patient)
    current = _reparse(unit, recognized_value=recognized_value, moved=moved)
    rows = list(LabObservation.objects.filter(parsing_version=current.parsing_version))
    assert len(rows) == 2
    assert all(row.manual_conflict for row in rows)
    assert effective_rows(patient) == ()
    workspace = report_workspace(patient)['current']
    assert len(workspace['rows']) == 2 and workspace['problems']
    with pytest.raises(ValidationError):
        submit_report_workspace(patient, patient.account, workspace['key'], workspace['token'],
                                uuid.uuid4(), {}, confirm=True)
    assert LabObservation.objects.filter(manual_identity=original.manual_identity).count() == 2


@pytest.mark.parametrize('winner_value', ['7', '8'])
def test_manual_conflict_resolution_keeps_one_result_and_survives_next_reparse(django_user_model,
                                                                               winner_value):
    _, patient = _patient(django_user_model, 'manual-reparse-resolve-' + winner_value)
    unit, original = _manual_report(patient)
    current = _reparse(unit, recognized_value='8')
    workspace = report_workspace(patient)['current']
    winner = next(row for row in workspace['rows'] if row.raw_value == winner_value)
    result = submit_report_workspace(patient, patient.account, workspace['key'], workspace['token'],
        uuid.uuid4(), {'resolutions': [{'manual_identity': str(original.manual_identity),
                                       'winner_id': str(winner.pk)}]}, confirm=True)
    assert result['confirmed'] is True
    retained, = effective_rows(patient)
    assert retained.raw_value == winner_value
    assert not report_workspace(patient)['current']['problems']
    assert LabObservation.objects.filter(parsing_version=current.parsing_version,
                                         revisions__action='RECONCILE').distinct().count() == 2

    newest = _reparse(current)
    retained, = effective_rows(patient)
    assert retained.parsing_version_id == newest.parsing_version_id
    assert retained.raw_value == winner_value
    assert retained.manual_identity == original.manual_identity
