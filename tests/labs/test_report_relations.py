from dataclasses import replace
from datetime import date, datetime

import pytest
from django.core.exceptions import PermissionDenied

from apps.labs import reports
from apps.labs.report_identity import extract_report_units
from apps.labs.reports import (
    ReportDecisionConflict, report_relations, persist_report_units, decide_relation,
    correct_report, effective_report,
)
from apps.labs.models import LabReportUnit, ReportAssociation, ReportAssociationEvent, LabReportRevision
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation
from tests.labs.test_report_identity import page


pytestmark = pytest.mark.django_db


def report(patient, *, number='A100', at='2026-09-17 08:30', value='5', person='合成人甲'):
    document, row = _observation(patient, date(2026, 9, 17), value)
    units = extract_report_units((page('合成医院', '检验报告', f'报告号：{number}',
                                      f'采样时间：{at}', f'姓名：{person}', f'白细胞 {value}'),))
    result = persist_report_units(row.parsing_version, units)
    row.refresh_from_db()
    return document, row, result[0]


def test_same_report_auto_relation_is_idempotent_and_preserves_sources(django_user_model):
    _, patient = _patient(django_user_model, 'report-auto')
    left_doc, left_row, left = report(patient)
    right_doc, right_row, right = report(patient)
    relations = report_relations(patient)
    assert len(relations) == 1 and relations[0].state == 'AUTO'
    assert report_relations(patient)[0].pk == relations[0].pk
    assert ReportAssociation.objects.count() == 1
    assert ReportAssociationEvent.objects.count() == 1
    assert left_row.report_unit_id == left.pk and right_row.report_unit_id == right.pk
    assert left_doc.pk != right_doc.pk
    assert left_doc.pages.count() == right_doc.pages.count() == 1


@pytest.mark.parametrize('changes,expected', [
    ({'number': 'B200'}, None), ({'number': ''}, None),
    ({'at': '2026-09-17 09:30'}, 'REVIEW'),
    ({'value': '6'}, 'REVIEW'), ({'person': '合成人乙'}, 'REVIEW'),
])
def test_identity_or_overlap_conflicts_are_never_automatically_joined(django_user_model, changes, expected):
    _, patient = _patient(django_user_model, 'report-conflict')
    report(patient)
    report(patient, **changes)
    relations = report_relations(patient)
    assert [item.state for item in relations] == ([] if expected is None else [expected])


def test_undo_blocks_same_evidence_and_stale_decisions(django_user_model):
    _, patient = _patient(django_user_model, 'report-undo')
    report(patient)
    report(patient)
    relation = report_relations(patient)[0]
    changed = decide_relation(patient, patient.account, relation.pk, 'UNDO', expected_revision=relation.revision_number,
                              rationale='对照两张原件后撤销', operation_id='undo-1')
    assert changed.state == 'UNDONE'
    assert report_relations(patient)[0].state == 'UNDONE'
    assert decide_relation(patient, patient.account, relation.pk, 'UNDO', expected_revision=relation.revision_number,
                           rationale='对照两张原件后撤销', operation_id='undo-1').pk == changed.pk
    assert ReportAssociationEvent.objects.count() == 2
    with pytest.raises(ReportDecisionConflict):
        decide_relation(patient, patient.account, relation.pk, 'SAME', expected_revision=relation.revision_number,
                        rationale='旧页面', operation_id='same-2')


def test_relation_rechecks_changed_evidence_without_overriding_human_decision(django_user_model):
    _, patient = _patient(django_user_model, 'report-recheck')
    report(patient)
    _, row, _ = report(patient)
    relation = report_relations(patient)[0]
    decide_relation(patient, patient.account, relation.pk, 'DIFFERENT', expected_revision=relation.revision_number,
                    rationale='原件不同', operation_id='different-1')
    row.raw_value = '6'
    row.save(update_fields=['raw_value'])
    assert report_relations(patient)[0].state == 'REVIEW'
    assert list(ReportAssociationEvent.objects.values_list('action', flat=True)) == ['AUTO', 'DIFFERENT', 'EVIDENCE_CHANGED']


def test_patient_and_deleted_source_boundaries(django_user_model):
    from django.utils import timezone
    _, patient = _patient(django_user_model, 'report-owner')
    _, other = _patient(django_user_model, 'report-other')
    report(patient)
    second, _, _ = report(patient)
    report(other)
    relation = report_relations(patient)[0]
    assert report_relations(other) == ()
    with pytest.raises(PermissionDenied):
        decide_relation(patient, other.account, relation.pk, 'SAME', expected_revision=relation.revision_number,
                        rationale='无权操作', operation_id='foreign')
    second.deleted_at = timezone.now()
    second.save(update_fields=['deleted_at'])
    assert report_relations(patient) == ()


def test_report_correction_requires_original_evidence_and_version(django_user_model):
    _, patient = _patient(django_user_model, 'report-correct')
    _, _, unit = report(patient)
    with pytest.raises(ValueError):
        correct_report(patient, patient.account, unit.pk, {'sampled_at': '2026-09-17 10:30'},
                       expected_revision=0, source_evidence={}, rationale='任意手填', operation_id='bad')
    corrected = correct_report(patient, patient.account, unit.pk, {'sampled_at': '2026-09-17 10:30'},
        expected_revision=0, source_evidence={'page_number': 1, 'polygon': [[.1, .1], [.9, .1], [.9, .2], [.1, .2]]},
        rationale='核对原件时间', operation_id='correct-1')
    assert effective_report(corrected).sampling_label == '2026-09-17 10:30'
    unit.refresh_from_db()
    assert datetime.fromisoformat(unit.automatic['sampled_at']) == datetime(2026, 9, 17, 8, 30)
    assert LabReportRevision.objects.count() == 1
    with pytest.raises(ReportDecisionConflict):
        correct_report(patient, patient.account, unit.pk, {'institution': '另一医院'},
            expected_revision=0, source_evidence={'page_number': 1, 'polygon': None}, rationale='旧页面', operation_id='stale')


def test_repeated_persistence_keeps_automatic_evidence_and_revisions(django_user_model):
    _, patient = _patient(django_user_model, 'report-retry')
    _, row, unit = report(patient)
    identity = effective_report(unit)
    assert persist_report_units(row.parsing_version, (identity,))[0].pk == unit.pk
    assert LabReportUnit.objects.count() == 1
    with pytest.raises(ReportDecisionConflict):
        persist_report_units(row.parsing_version, (replace(identity, report_number='CHANGED'),))


def test_report_correction_cannot_borrow_a_different_region_of_the_same_page(django_user_model):
    _, patient = _patient(django_user_model, 'report-wrong-region')
    _, _, unit = report(patient)
    with pytest.raises(ValueError):
        correct_report(patient, patient.account, unit.pk, {'sampled_at': '2026-09-17 10:30'},
            expected_revision=0, source_evidence={'page_number': 1, 'polygon': [[.1, .8], [.9, .8], [.9, .9], [.1, .9]]},
            rationale='另一区域的时间不能挪用', operation_id='wrong-region')
    assert not LabReportRevision.objects.exists()


def test_ten_photos_of_one_report_have_bounded_relation_refresh_queries(django_user_model):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _, patient = _patient(django_user_model, 'report-query-budget')
    for _ in range(10):
        report(patient)
    assert len(report_relations(patient)) == 45
    with CaptureQueriesContext(connection) as captured:
        refreshed = report_relations(patient)
    assert len(refreshed) == 45 and all(item.state == 'AUTO' for item in refreshed)
    assert len(captured) <= 40


def test_organize_new_pair_saves_one_decision_and_retry_is_idempotent(django_user_model):
    _, patient = _patient(django_user_model, 'organize-new-pair')
    _, _, left = report(patient, number='')
    _, _, right = report(patient, number='')
    token = reports.report_organization_token(patient)

    payload = {'expected_context': token, 'rationale': '对照原件确认续页相同',
               'operation_id': 'organize-new-pair-1'}
    saved = reports.organize_report_relation(patient, patient.account, left.pk, str(left.pk), str(right.pk),
                                              'SAME', **payload)
    repeated = reports.organize_report_relation(patient, patient.account, left.pk, str(left.pk), str(right.pk),
                                                 'SAME', **payload)

    assert saved.pk == repeated.pk
    assert saved.state == 'SAME'
    assert ReportAssociation.objects.count() == 1
    assert ReportAssociationEvent.objects.filter(action='SAME').count() == 1
    assert list(saved.events.values_list('rationale', flat=True))[-1] == '对照原件确认续页相同'


def test_organize_rejects_stale_context_without_creating_another_pair(django_user_model):
    _, patient = _patient(django_user_model, 'organize-stale-pair')
    units = [report(patient, number='')[2] for _ in range(3)]
    stale = reports.report_organization_token(patient)
    reports.organize_report_relation(patient, patient.account, units[0].pk, units[0].pk, units[1].pk,
        'SAME', expected_context=stale, rationale='前两份是同一报告', operation_id='organize-first')

    with pytest.raises(ReportDecisionConflict, match='变化'):
        reports.organize_report_relation(patient, patient.account, units[0].pk, units[0].pk, units[2].pk,
            'SAME', expected_context=stale, rationale='过期页面', operation_id='organize-stale')
    assert ReportAssociation.objects.count() == 1


def test_organize_rejects_changed_result_evidence_even_without_a_proposed_pair(django_user_model):
    from apps.labs.revisions import revise_observation

    _, patient = _patient(django_user_model, 'organize-result-changed')
    _, _, left = report(patient, number='')
    _, row, right = report(patient, number='')
    token = reports.report_organization_token(patient)
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '6'},
                       expected_revision=0)

    with pytest.raises(ReportDecisionConflict, match='变化'):
        reports.organize_report_relation(patient, patient.account, left.pk, left.pk, right.pk, 'SAME',
            expected_context=token, rationale='过期原件依据', operation_id='organize-old-result')
    assert not ReportAssociation.objects.exists()


def test_organize_undo_changes_only_selected_relation(django_user_model):
    _, patient = _patient(django_user_model, 'organize-one-relation')
    units = [report(patient)[2] for _ in range(3)]
    relations = report_relations(patient)
    selected = next(item for item in relations if {item.left_key, item.right_key} ==
                    {units[0].source_key, units[2].source_key})

    saved = reports.organize_report_relation(patient, patient.account, units[0].pk, units[0].pk, units[2].pk,
        'UNDO', expected_context=reports.report_organization_token(patient),
        rationale='两份原件属于不同报告', operation_id='organize-one-undo')

    assert saved.pk == selected.pk and saved.state == 'UNDONE'
    assert sorted(item.state for item in report_relations(patient)) == ['AUTO', 'AUTO', 'UNDONE']


def test_organize_rejects_foreign_deleted_and_same_source(django_user_model):
    from django.utils import timezone

    _, patient = _patient(django_user_model, 'organize-scope-owner')
    _, other = _patient(django_user_model, 'organize-scope-other')
    _, _, left = report(patient, number='')
    document, _, right = report(patient, number='')
    _, _, foreign = report(other, number='')
    token = reports.report_organization_token(patient)
    payload = {'expected_context': token, 'rationale': '核对原件', 'operation_id': 'organize-scope'}

    with pytest.raises(PermissionDenied):
        reports.organize_report_relation(patient, patient.account, left.pk, left.pk, foreign.pk, 'SAME', **payload)
    with pytest.raises(ValueError):
        reports.organize_report_relation(patient, patient.account, left.pk, left.pk, left.pk, 'SAME', **payload)
    with pytest.raises(PermissionDenied):
        reports.organize_report_relation(patient, other.account, left.pk, left.pk, right.pk, 'SAME', **payload)
    document.deleted_at = timezone.now()
    document.save(update_fields=['deleted_at'])
    with pytest.raises(ReportDecisionConflict, match='变化'):
        reports.organize_report_relation(patient, patient.account, left.pk, left.pk, right.pk, 'SAME', **payload)
    assert ReportAssociation.objects.count() == 0


def test_organize_different_decision_stays_resolved_for_same_evidence(django_user_model):
    _, patient = _patient(django_user_model, 'organize-different')
    _, _, left = report(patient)
    _, _, right = report(patient, at='2026-09-17 09:30')
    relation, = report_relations(patient)
    assert relation.state == 'REVIEW'

    decided = reports.organize_report_relation(patient, patient.account, left.pk, left.pk, right.pk,
        'DIFFERENT', expected_context=reports.report_organization_token(patient),
        rationale='采样时间不同，原图属于两份报告', operation_id='different-pair')

    assert decided.state == 'DIFFERENT'
    assert report_relations(patient)[0].state == 'DIFFERENT'
    assert ReportAssociationEvent.objects.filter(action='DIFFERENT').count() == 1


def test_organize_requires_short_rationale_and_current_parse(django_user_model):
    _, patient = _patient(django_user_model, 'organize-invalid')
    _, _, left = report(patient, number='')
    _, _, right = report(patient, number='')
    token = reports.report_organization_token(patient)

    with pytest.raises(ValueError):
        reports.organize_report_relation(patient, patient.account, left.pk, left.pk, right.pk, 'SAME',
            expected_context=token, rationale='依据' * 1001, operation_id='too-long')
    assert not ReportAssociation.objects.exists()
    right.parsing_version.active = False
    right.parsing_version.save(update_fields=['active'])
    with pytest.raises(ReportDecisionConflict, match='解析版本'):
        reports.organize_report_relation(patient, patient.account, left.pk, left.pk, right.pk, 'SAME',
            expected_context=token, rationale='核对原件', operation_id='old-parse')
    assert not ReportAssociation.objects.exists()
