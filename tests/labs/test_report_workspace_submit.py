import uuid
from copy import copy

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from apps.labs.models import LabReportReviewEvent
from apps.labs.report_workspace import report_workspace, submit_report_workspace
from apps.labs.revisions import RevisionConflict, revise_observation
from apps.labs.revisions import effective_observation
from apps.labs.readmodels import effective_rows
from apps.labs.reports import effective_report
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


def _current(patient):
    return report_workspace(patient)['current']


def _submit(patient, current, *, operation_id=None, confirm=True, edits=None):
    return submit_report_workspace(patient, patient.account, current['key'], current['token'],
                                   operation_id or uuid.uuid4(), edits or {}, confirm=confirm)


@pytest.mark.parametrize('decision', ['KEEP_REVISION', 'USE_AUTOMATIC'])
def test_report_revision_conflict_can_be_resolved_in_current_workspace(django_user_model, decision):
    from dataclasses import replace
    from apps.labs.reports import _from_snapshot, correct_report
    from tests.labs.test_report_revision_versions import next_report_version, SOURCE

    _, patient = _patient(django_user_model, 'workspace-report-resolution-' + decision)
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '人工核对医院'},
                   expected_revision=0, source_evidence=SOURCE, rationale='核对原件', operation_id='first')
    changed = replace(_from_snapshot(original.automatic), report_number='CHANGED')
    current_unit, = next_report_version(original, (changed,))
    current = _current(patient)
    assert '报告信息或修订存在冲突' in current['problems']

    _submit(patient, current, edits={'report_resolutions': [{'unit_id': str(current_unit.pk),
        'expected_revision': 0, 'decision': decision}]}, confirm=False)

    current_unit.refresh_from_db()
    assert current_unit.revisions.get().action == decision
    assert current_unit.revisions.get().inherited_from_id == original.revisions.get().pk
    assert effective_report(current_unit).institution == (
        '人工核对医院' if decision == 'KEEP_REVISION' else changed.institution)
    assert '报告信息或修订存在冲突' not in _current(patient)['problems']


def test_one_row_can_be_corrected_and_excluded_in_one_save(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-edit-exclude')
    _, row, _ = report(patient)
    current = _current(patient)

    _submit(patient, current, confirm=False, edits={'observations': [{
        'id': str(row.pk), 'expected_revision': 0, 'action': 'EXCLUDE',
        'changes': {'raw_name': '原件错误识别的项目'}, 'reason': 'MISRECOGNIZED'}]})

    row.refresh_from_db()
    assert [event.action for event in row.revisions.order_by('sequence')] == ['CORRECT', 'EXCLUDE']
    assert effective_observation(row).excluded
    assert effective_observation(row).raw_name == '原件错误识别的项目'


def test_falsely_recognized_optional_report_number_can_be_cleared(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-clear-report-number')
    _, _, unit = report(patient)
    current = _current(patient)

    _submit(patient, current, confirm=False, edits={'reports': [{
        'unit_id': str(unit.pk), 'expected_revision': 0, 'changes': {'report_number': ''}}]})

    unit.refresh_from_db()
    assert effective_report(unit).report_number == ''


def test_falsely_recognized_sampling_time_can_be_cleared_without_inventing_a_date(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-clear-sampling-time')
    _, _, unit = report(patient)
    current = _current(patient)

    _submit(patient, current, confirm=False, edits={'reports': [{
        'unit_id': str(unit.pk), 'expected_revision': 0, 'changes': {'sampled_at': ''}}]})

    unit.refresh_from_db()
    identity = effective_report(unit)
    assert identity.sampled_at is None and identity.status == 'REJECTED'


def test_confirm_single_zero_observation_report_and_retry_without_duplicate_event(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-confirm-zero')
    _, row, unit = report(patient)
    row.delete()
    current = _current(patient)
    assert current['units'] == (unit,) and current['rows'] == ()
    operation = uuid.uuid4()

    first = _submit(patient, current, operation_id=operation)
    again = _submit(patient, current, operation_id=operation)

    assert first == again
    assert first['confirmed'] is True
    assert LabReportReviewEvent.objects.filter(patient=patient, action='CONFIRM').count() == 1
    assert _current(patient)['confirmed'] is True


def test_save_does_not_confirm_and_later_edit_invalidates_previous_confirmation(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-confirm-stale')
    _, row, _ = report(patient)
    current = _current(patient)
    saved = _submit(patient, current, confirm=False)
    assert saved['confirmed'] is False
    assert _current(patient)['confirmed'] is False

    _submit(patient, _current(patient))
    assert _current(patient)['confirmed'] is True
    row.refresh_from_db()
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '7'},
                       expected_revision=row.revision_number)
    assert _current(patient)['confirmed'] is False
    assert LabReportReviewEvent.objects.filter(patient=patient, action='CONFIRM').count() == 1


def test_stale_token_and_reused_operation_with_different_payload_do_not_write(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-submit-stale')
    _, row, _ = report(patient)
    current = _current(patient)
    operation = uuid.uuid4()
    _submit(patient, current, operation_id=operation)
    with pytest.raises(RevisionConflict):
        _submit(patient, current, operation_id=operation, confirm=False)
    assert LabReportReviewEvent.objects.count() == 1

    row.refresh_from_db()
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '7'},
                       expected_revision=row.revision_number)
    with pytest.raises(RevisionConflict):
        _submit(patient, current)
    assert LabReportReviewEvent.objects.count() == 1


def test_report_scope_and_permission_checked_server_side(django_user_model):
    from apps.patients.models import PatientMembership

    _, patient = _patient(django_user_model, 'workspace-submit-owner')
    _, other = _patient(django_user_model, 'workspace-submit-other')
    _, row, _ = report(patient)
    _, foreign, _ = report(other)
    current = _current(patient)
    with pytest.raises((PermissionDenied, ValidationError)):
        _submit(patient, current, edits={'observations': [{'id': str(foreign.pk), 'raw_value': '9'}]})
    assert not LabReportReviewEvent.objects.exists()

    PatientMembership.objects.create(patient=patient, account=other.account, role='VIEWER')
    with pytest.raises(PermissionDenied):
        submit_report_workspace(patient, other.account, current['key'], current['token'], uuid.uuid4(), {}, confirm=True)
    assert not row.revisions.exists()


def test_multiple_observation_fields_save_atomically_without_confirming(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-multi-edit')
    _, first, _ = report(patient)
    second = copy(first)
    second.pk, second.reading_order = uuid.uuid4(), first.reading_order + 1
    second.save(force_insert=True)
    current = _current(patient)
    result = _submit(patient, current, confirm=False, edits={'observations': [
        {'id': str(first.pk), 'expected_revision': 0,
         'changes': {'raw_value': '<3', 'raw_unit': '', 'reference_range_raw': '', 'report_flag_raw': '+'}},
        {'id': str(second.pk), 'expected_revision': 0,
         'changes': {'raw_name': '未收录合成项目', 'raw_value': '阴性', 'specimen': '', 'method_raw': '人工法'}},
    ]})
    assert result['confirmed'] is False
    first.refresh_from_db()
    second.refresh_from_db()
    effective_first, effective_second = effective_observation(first), effective_observation(second)
    assert (effective_first.raw_value, effective_first.result_type, effective_first.raw_unit,
            effective_first.reference_range_raw, effective_first.report_flag_raw) == ('<3', 'COMPARATOR', '', '', '+')
    assert (effective_second.raw_name, effective_second.raw_value, effective_second.method_raw) == (
        '未收录合成项目', '阴性', '人工法')
    assert effective_second.standard_code.startswith('CANDIDATE_')
    assert not _current(patient)['confirmed']


def test_invalid_second_observation_rolls_back_first_and_confirmation(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-atomic-edit')
    _, first, _ = report(patient)
    second = copy(first)
    second.pk, second.reading_order = uuid.uuid4(), first.reading_order + 1
    second.save(force_insert=True)
    current = _current(patient)
    with pytest.raises(ValidationError):
        _submit(patient, current, edits={'observations': [
            {'id': str(first.pk), 'expected_revision': 0, 'changes': {'raw_value': '7'}},
            {'id': str(second.pk), 'expected_revision': 0, 'changes': {'raw_value': ''}},
        ]})
    assert not first.revisions.exists() and not second.revisions.exists()
    assert not LabReportReviewEvent.objects.exists()


def test_correction_accepts_report_status_text_without_numeric_invention(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-status-correction')
    _, row, _ = report(patient)
    _submit(patient, _current(patient), confirm=False, edits={'observations': [
        {'id': str(row.pk), 'expected_revision': 0, 'changes': {'raw_value': '待复检'}},
    ]})
    effective = effective_observation(row)
    assert effective.raw_value == '待复检' and effective.result_type == 'STATUS'


def test_workspace_keeps_per_result_phase_edit(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-phase')
    _, row, _ = report(patient)
    _submit(patient, _current(patient), confirm=False, edits={'observations': [
        {'id': str(row.pk), 'expected_revision': 0, 'changes': {'physiological_phase': '黄体期'}},
    ]})
    assert effective_observation(row).physiological_phase == '黄体期'
    assert row.physiological_phase == ''


def test_cross_report_observation_rejected_even_for_same_patient(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-cross-report')
    _, own, _ = report(patient)
    _, other, _ = report(patient, number='B200')
    current = next(item for item in report_workspace(patient)['reports'] if own.pk in {row.pk for row in item['rows']})
    with pytest.raises((PermissionDenied, ValidationError)):
        _submit(patient, current, edits={'observations': [
            {'id': str(other.pk), 'expected_revision': 0, 'changes': {'raw_value': '9'}},
        ]})
    assert not LabReportReviewEvent.objects.exists()


def test_confirm_includes_current_edits_only_in_selected_report(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-confirm-edit')
    _, own, _ = report(patient)
    _, other, _ = report(patient, number='B200')
    current = next(item for item in report_workspace(patient)['reports'] if own.pk in {row.pk for row in item['rows']})
    result = _submit(patient, current, edits={'observations': [
        {'id': str(own.pk), 'expected_revision': 0, 'changes': {'raw_value': '7'}},
    ]})
    assert result['confirmed'] is True
    assert effective_observation(own).raw_value == '7'
    after = {item['key']: item for item in report_workspace(patient)['reports']}
    assert after[current['key']]['confirmed'] is True
    assert not any(item['confirmed'] for key, item in after.items() if key != current['key'])
    assert not other.revisions.exists()


def test_exclude_and_restore_keep_source_and_history_but_change_shared_effective_reads(django_user_model):
    from apps.exports.content import build_snapshot

    _, patient = _patient(django_user_model, 'workspace-exclusion')
    _, row, _ = report(patient)
    current = _current(patient)
    _submit(patient, current, edits={'observations': [
        {'id': str(row.pk), 'expected_revision': 0, 'action': 'EXCLUDE', 'reason': 'DUPLICATE'},
    ]})
    row.refresh_from_db()
    assert row.raw_value == '5' and row.revisions.count() == 1
    assert effective_observation(row).excluded is True
    assert effective_rows(patient) == ()
    assert build_snapshot(patient, {'mode': 'all'})['lab_results'] == []
    excluded = _current(patient)
    assert len(excluded['rows']) == 1 and excluded['rows'][0].excluded
    assert _submit(patient, excluded)['confirmed'] is True

    row.refresh_from_db()
    _submit(patient, _current(patient), confirm=False, edits={'observations': [
        {'id': str(row.pk), 'expected_revision': row.revision_number, 'action': 'RESTORE'},
    ]})
    assert len(effective_rows(patient)) == 1
    assert _current(patient)['confirmed'] is False
    assert list(row.revisions.values_list('action', flat=True)) == ['EXCLUDE', 'RESTORE']


def test_exclusion_requires_reason_and_does_not_accept_other_report_row(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-exclusion-invalid')
    _, own, _ = report(patient)
    _, foreign, _ = report(patient, number='B200')
    current = next(item for item in report_workspace(patient)['reports'] if own.pk in {row.pk for row in item['rows']})
    for item in (
        {'id': str(own.pk), 'expected_revision': 0, 'action': 'EXCLUDE'},
        {'id': str(own.pk), 'expected_revision': 0, 'action': 'EXCLUDE', 'reason': 'UNDEFINED'},
        {'id': str(foreign.pk), 'expected_revision': 0, 'action': 'EXCLUDE', 'reason': 'DUPLICATE'},
    ):
        with pytest.raises((ValidationError, PermissionDenied)):
            _submit(patient, current, edits={'observations': [item]})
    assert not own.revisions.exists() and not foreign.revisions.exists()


def test_report_fields_and_observation_can_be_saved_together_with_original_source(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-report-fields')
    _, row, unit = report(patient)
    current = _current(patient)
    _submit(patient, current, confirm=False, edits={
        'reports': [{'unit_id': str(unit.pk), 'expected_revision': 0,
                     'changes': {'institution': '合成复核医院', 'sampled_at': '2026-09-17 10:30'}}],
        'observations': [{'id': str(row.pk), 'expected_revision': 0, 'changes': {'raw_value': '7'}}],
    })
    unit.refresh_from_db()
    assert effective_report(unit).institution == '合成复核医院'
    assert effective_report(unit).sampling_label == '2026-09-17 10:30'
    assert unit.automatic['institution'] == '合成医院'
    event = unit.revisions.get()
    assert event.source_evidence['page_number'] == unit.document_page.page_number
    assert event.source_evidence['origin'] == 'MANUAL'
    assert event.after['fields']['institution'][0]['origin'] == 'USER'
    assert event.after['fields']['institution'][0]['confidence'] is None
    assert effective_observation(row).raw_value == '7'
    saved = LabReportReviewEvent.objects.get()
    assert saved.basis['report'][0]['identity']['institution'] == '合成复核医院'


def test_invalid_report_field_rolls_back_observation_edit_and_no_event(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-report-field-invalid')
    _, row, unit = report(patient)
    current = _current(patient)
    with pytest.raises((ValidationError, ValueError)):
        _submit(patient, current, confirm=True, edits={
            'observations': [{'id': str(row.pk), 'expected_revision': 0, 'changes': {'raw_value': '7'}}],
            'reports': [{'unit_id': str(unit.pk), 'expected_revision': 0,
                         'changes': {'sampled_at': '2026-09-17'}}],
        })
    assert not unit.revisions.exists() and not row.revisions.exists()
    assert not LabReportReviewEvent.objects.exists()


def test_report_field_edit_cannot_target_another_report_or_foreign_source(django_user_model):
    _, patient = _patient(django_user_model, 'workspace-report-field-scope')
    _, other = _patient(django_user_model, 'workspace-report-field-other')
    _, row, unit = report(patient)
    _, _, other_unit = report(patient, number='B200')
    _, _, foreign_unit = report(other)
    current = next(item for item in report_workspace(patient)['reports'] if row.pk in {item.pk for item in item['rows']})
    for target in (other_unit, foreign_unit):
        with pytest.raises((ValidationError, PermissionDenied)):
            _submit(patient, current, edits={'reports': [
                {'unit_id': str(target.pk), 'expected_revision': 0,
                 'changes': {'institution': '不应写入'}},
            ]})
    assert not unit.revisions.exists() and not other_unit.revisions.exists() and not foreign_unit.revisions.exists()


@pytest.mark.parametrize('value,kind', [('<3', 'COMPARATOR'), ('阴性', 'QUALITATIVE'),
                                        ('++', 'SEMI_QUANTITATIVE'), ('待复检', 'STATUS')])
def test_manual_observation_in_zero_row_report_preserves_source_and_retry(django_user_model, value, kind):
    from apps.labs.models import LabObservation

    _, patient = _patient(django_user_model, 'workspace-add-' + kind)
    _, old, unit = report(patient)
    old.delete()
    current = _current(patient)
    operation = uuid.uuid4()
    edits = {'additions': [{'unit_id': str(unit.pk), 'raw_name': '合成目录外项目', 'raw_value': value,
                            'raw_unit': '', 'reference_range_raw': '', 'report_flag_raw': '',
                            'specimen': '', 'method_raw': ''}]}
    first = _submit(patient, current, operation_id=operation, confirm=False, edits=edits)
    again = _submit(patient, current, operation_id=operation, confirm=False, edits=edits)
    assert first == again
    row, = LabObservation.objects.filter(manual_identity__isnull=False)
    assert row.report_unit_id == unit.pk and row.document_page_id == unit.document_page_id
    assert row.manual_created_by_id == patient.account_id
    assert row.manual_identity and row.result_type == kind
    assert row.raw_name == '合成目录外项目' and row.raw_value == value
    assert row.standard_code.startswith('CANDIDATE_')
    assert row.evidence.origin == 'MANUAL' and row.evidence.ocr_block_id is None
    assert row.evidence.confidence is None and row.evidence.polygon is None
    assert len(_current(patient)['rows']) == 1
    assert len(effective_rows(patient)) == 1


def test_manual_addition_invalid_fields_or_foreign_unit_roll_back(django_user_model):
    from apps.labs.models import LabObservation

    _, patient = _patient(django_user_model, 'workspace-add-invalid')
    _, other = _patient(django_user_model, 'workspace-add-foreign')
    _, own, unit = report(patient)
    _, _, foreign_unit = report(other)
    current = _current(patient)
    for addition in (
        {'unit_id': str(unit.pk), 'raw_name': '', 'raw_value': '5'},
        {'unit_id': str(unit.pk), 'raw_name': '合成项目', 'raw_value': ''},
        {'unit_id': str(foreign_unit.pk), 'raw_name': '合成项目', 'raw_value': '5'},
    ):
        with pytest.raises((ValidationError, PermissionDenied)):
            _submit(patient, current, edits={'additions': [addition], 'observations': [
                {'id': str(own.pk), 'expected_revision': 0, 'changes': {'raw_value': '7'}},
            ]})
    assert not LabObservation.objects.filter(manual_identity__isnull=False).exists()
    assert not own.revisions.exists() and not LabReportReviewEvent.objects.exists()
