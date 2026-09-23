import pytest
from django.core.exceptions import ValidationError

from apps.labs.models import RevisionAction
from apps.labs.revisions import effective_observation, revise_observation
from tests.labs.test_phase_two_workflows import case


pytestmark = pytest.mark.django_db


def test_manual_phase_is_per_result_and_preserves_raw_value_and_review_gate(case):
    _, patient, _, row = case
    row.quality_issues = [{'code': 'recognition_uncertain', 'fields': ['raw_value']}]
    row.save(update_fields=['quality_issues'])
    revise_observation(patient.account, row.pk, action=RevisionAction.REPORT_ERROR, changes={}, expected_revision=0)
    revise_observation(patient.account, row.pk, action=RevisionAction.CORRECT,
                       changes={'physiological_phase': '卵泡期'}, expected_revision=1)
    row.refresh_from_db()
    current = effective_observation(row)
    assert current.physiological_phase == '卵泡期'
    assert current.raw_value == row.raw_value
    assert current.reported_error
    assert any(item['code'] == 'recognition_uncertain' for item in current.quality_issues)
    assert row.physiological_phase == ''
    assert current.value_sources['physiological_phase']['revision_id']
    revise_observation(patient.account, row.pk, action=RevisionAction.UNDO, changes={}, expected_revision=2)
    row.refresh_from_db()
    assert effective_observation(row).physiological_phase == ''


@pytest.mark.parametrize('phase', ['未注明', '卵泡期或黄体期', '根据年龄推测绝经'])
def test_unknown_or_multiple_phases_cannot_be_saved(case, phase):
    _, patient, _, row = case
    with pytest.raises(ValidationError):
        revise_observation(patient.account, row.pk, action=RevisionAction.CORRECT,
                           changes={'physiological_phase': phase}, expected_revision=0)
    assert not row.revisions.exists()


def test_phase_can_be_cleared_without_changing_the_result(case):
    _, patient, _, row = case
    revise_observation(patient.account, row.pk, action=RevisionAction.CORRECT,
                       changes={'physiological_phase': '黄体期'}, expected_revision=0)
    revise_observation(patient.account, row.pk, action=RevisionAction.CORRECT,
                       changes={'physiological_phase': ''}, expected_revision=1)
    row.refresh_from_db()
    current = effective_observation(row)
    assert current.physiological_phase == '' and current.raw_value == row.raw_value


def test_patient_can_add_phase_through_existing_revision_endpoint(case):
    client, _, _, row = case
    response = client.post(f'/labs/observations/{row.pk}/', {
        'action': 'CORRECT', 'physiological_phase': '黄体期', 'expected_revision': '0',
    })
    assert response.status_code == 302
    row.refresh_from_db()
    assert effective_observation(row).physiological_phase == '黄体期'


@pytest.mark.parametrize('text,expected', [
    ('本次阶段：卵泡期', '卵泡期'), ('月经阶段：黄体期', '黄体期'),
    ('生理阶段：排卵期', '排卵期'), ('患者阶段：绝经期', '绝经期'),
    ('卵泡期 3.85-8.78 排卵期 4.54-22.51 黄体期 1.79-5.12', ''),
    ('本次阶段：卵泡期或黄体期', ''), ('参考范围：卵泡期 3.85-8.78', ''),
])
def test_report_phase_requires_explicit_patient_statement(text, expected):
    from tests.labs.test_report_identity import unit
    from apps.labs.phases import report_phase
    report = unit('采样时间：2026-09-22 08:30', text)
    phase, raw, evidence = report_phase(report)
    assert phase == expected
    if expected:
        assert raw == text
        assert evidence[0]['polygon']


def test_report_phase_is_persisted_with_original_evidence(case):
    from apps.labs.reports import persist_report_units
    from tests.labs.test_report_identity import unit
    _, _, _, row = case
    report = unit('采样时间：2026-09-22 08:30', '本次阶段：黄体期')
    persist_report_units(row.parsing_version, (report,))
    row.refresh_from_db()
    assert row.physiological_phase == '黄体期'
    assert row.phase_raw == '本次阶段：黄体期'
    assert row.field_evidence['physiological_phase']['polygon']


def test_each_historical_result_uses_its_own_phase_and_can_be_edited(case):
    from copy import copy
    from uuid import uuid4
    from apps.labs.comparison import comparison_view
    from apps.labs.reports import persist_report_units
    from tests.labs.test_report_identity import unit
    client, patient, _, row = case
    patient.sex = 'F'
    patient.save()
    row.raw_name = row.standard_name = '促卵泡生成激素'
    row.standard_code, row.raw_unit, row.raw_value = 'CANDIDATE_FSH', 'IU/L', '6'
    row.specimen = 'BLOOD'
    row.save()
    persist_report_units(row.parsing_version, (unit('采样时间：2026-09-22 08:30', '本次阶段：卵泡期'),))
    # A second result's manually recorded phase must not become a patient attribute.
    second = copy(row)
    second.pk, second.reading_order, second.physiological_phase = uuid4(), 2, '黄体期'
    second.save(force_insert=True)
    table = comparison_view(patient)
    fsh, = [item for item in table.rows if item.standard_name == '促卵泡生成激素']
    cells = [cell for entries in fsh.cells for cell in entries]
    assert {cell.standard_reference for cell in cells} == {'3.85–8.78', '1.79–5.12'}
    detail = client.get(f'/labs/observations/{row.pk}/').content.decode()
    assert 'name="physiological_phase"' in detail and '本次阶段：卵泡期' in detail


def test_different_report_units_do_not_share_phase():
    from apps.labs.phases import report_phase
    from apps.labs.report_identity import extract_report_units
    from tests.labs.test_report_identity import page
    reports = extract_report_units((page(
        '合成医院', '检验报告', '报告号：R1', '采样时间：2026-09-22 08:30', '本次阶段：卵泡期',
        '合成医院', '检验报告', '报告号：R2', '采样时间：2026-09-23 08:30',
    ),))
    assert len(reports) == 2
    assert [report_phase(report)[0] for report in reports] == ['卵泡期', '']


def test_first_historical_read_projects_phase_without_overriding_manual_clear(case, monkeypatch):
    from apps.labs.readmodels import effective_rows
    from tests.labs.test_report_identity import unit
    _, patient, _, row = case
    report = unit('采样时间：2026-09-22 08:30', '本次阶段：黄体期')
    monkeypatch.setattr('apps.labs.report_reads._historical_units',
        lambda rows: {(row.parsing_version_id, row.document_page_id): (report,)})
    current, = effective_rows(patient)
    assert current.physiological_phase == '黄体期'
    assert current.phase_raw == '本次阶段：黄体期'
    assert current.field_evidence['physiological_phase']['polygon']
    revise_observation(patient.account, row.pk, action=RevisionAction.CORRECT,
                       changes={'physiological_phase': ''}, expected_revision=0)
    cleared, = effective_rows(patient)
    assert cleared.physiological_phase == ''


@pytest.mark.parametrize('inside,confidence,expected', [
    (True, '.99', '黄体期'), (True, '.8', ''), (False, '.99', ''),
])
def test_existing_historical_unit_recovers_only_own_phase_evidence(case, inside, confidence, expected):
    from dataclasses import replace
    from apps.labs.readmodels import effective_rows
    from apps.labs.reports import persist_report_units
    from apps.processing.models import OcrBlock
    from tests.labs.test_report_identity import unit
    _, patient, _, row = case
    report = unit('采样时间：2026-09-22 08:30')
    fields = {key: value for key, value in report.fields.items() if key != 'physiological_phase'}
    report = replace(report, fields=fields, source_region=((0, 0), (1, 0), (1, .5), (0, .5)))
    saved, = persist_report_units(row.parsing_version, (report,))
    before = saved.automatic
    top = .2 if inside else .7
    OcrBlock.objects.create(parsing_version=row.parsing_version, document_page=row.document_page,
        reading_order=20, text='本次阶段：黄体期', confidence=confidence,
        polygon=[[.1, top], [.9, top], [.9, top + .05], [.1, top + .05]])
    current, = effective_rows(patient)
    assert current.physiological_phase == expected
    if inside:
        assert current.phase_raw == '本次阶段：黄体期'
    saved.refresh_from_db()
    assert saved.automatic == before
    assert row.parsing_version.ocr_blocks.count() == 1


def test_retry_of_pre_catalog_report_does_not_rewrite_its_original_identity(case):
    from dataclasses import replace
    from apps.labs.reports import persist_report_units, ReportDecisionConflict
    from tests.labs.test_report_identity import unit
    _, _, _, row = case
    current = unit('采样时间：2026-09-22 08:30')
    old = replace(current, fields={key: value for key, value in current.fields.items() if key != 'physiological_phase'})
    saved, = persist_report_units(row.parsing_version, (old,))
    before = saved.automatic
    repeated, = persist_report_units(row.parsing_version, (current,))
    assert repeated.pk == saved.pk
    saved.refresh_from_db()
    assert saved.automatic == before
    with pytest.raises(ReportDecisionConflict):
        persist_report_units(row.parsing_version, (replace(current, report_number='CHANGED'),))


def test_readonly_member_cannot_edit_phase_or_see_edit_form(case, django_user_model):
    from apps.patients.models import PatientMembership
    from tests.documents.test_detail_viewer import _patient
    _, patient, _, row = case
    row.raw_name, row.raw_unit = 'FSH', 'IU/L'
    row.save()
    viewer, other = _patient(django_user_model, 'phase-readonly')
    PatientMembership.objects.create(patient=patient, account=other.account, role='VIEWER')
    session = viewer.session
    session['active_patient_id'] = str(patient.pk)
    session.save()
    response = viewer.get(f'/labs/observations/{row.pk}/')
    assert response.status_code == 200
    assert 'name="physiological_phase"' not in response.content.decode()
    response = viewer.post(f'/labs/observations/{row.pk}/', {
        'action': 'CORRECT', 'physiological_phase': '卵泡期', 'expected_revision': '0'})
    assert response.status_code == 403
    assert not row.revisions.exists()
