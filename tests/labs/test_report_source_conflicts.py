from datetime import date

import pytest

from apps.exports.content import build_snapshot
from apps.labs.comparison import comparison_view
from apps.labs.readmodels import effective_rows
from apps.labs.reports import decide_relation, report_relations
from apps.labs.trends import trend_view
from apps.patients.sharing_content import project_snapshot
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('difference', ['time', 'patient', 'result'])
def test_conflicting_report_identity_blocks_daily_trend_but_keeps_sources(django_user_model, difference):
    client, patient = _patient(django_user_model, 'report-source-conflict-' + difference)
    report(patient, number='EARLIER', at='2026-09-16 08:30', value='2')
    first_doc, first, first_unit = report(patient, number='CURRENT', value='5')
    _, second, _ = report(patient, number='CURRENT', value='6' if difference == 'result' else '5',
        at='2026-09-17 10:30' if difference == 'time' else '2026-09-17 08:30',
        person='合成人乙' if difference == 'patient' else '合成人甲')
    trend = trend_view(patient, 'LAB_WBC', include_history=True)
    assert [point.observation.raw_value for series in trend.series for point in series.points] == ['2']
    assert {cell.observation.pk for cell in trend.disputed} == {first.pk, second.pk}
    assert sum(len(cell.sources) for cell in trend.daily_details) == 3
    table = comparison_view(patient)
    assert sum(len(cell.sources) for row in table.rows for column in row.cells for cell in column) == 3
    assert any(cell.review_required for row in table.rows for column in row.cells for cell in column)
    assert '报告归属存在冲突' in client.get('/labs/compare/').content.decode()
    for url in (f'/records/{first_doc.pk}/', f'/labs/observations/{first.pk}/', f'/labs/reports/{first_unit.pk}/'):
        response = client.get(url)
        assert response.status_code == 200
        assert '报告归属存在冲突' in response.content.decode()
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert {key for item in snapshot['lab_results'] if item['disputed'] for key in item['source_ids']} == {str(first.pk), str(second.pk)}
    assert {point['date'] for series in snapshot['card']['trends'] for point in series['points']} <= {'2026-09-16'}
    from apps.exports.formats import csv_tables
    import csv
    import io
    relations = list(csv.DictReader(io.StringIO(csv_tables(snapshot)['lab_report_relations.csv'].decode('utf-8-sig'))))
    assert any(row['conflict'] == 'true' for row in relations)


def test_partial_share_refolds_after_excluding_conflicting_identity(django_user_model):
    _, patient = _patient(django_user_model, 'report-conflict-share')
    first_doc, first, _ = report(patient, value='5.0')
    second_doc, second, _ = report(patient, value='5.00')
    other_doc, other, _ = report(patient, value='5', person='合成人乙')
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert len(snapshot['lab_results']) == 3
    assert all(item['disputed'] for item in snapshot['lab_results'])
    shared = project_snapshot(snapshot, {'sections': ['labs'],
        'document_ids': [str(document.pk) for document in (first_doc, second_doc, other_doc)],
        'lab_ids': [str(first.pk), str(second.pk)]})
    folded, = shared['lab_results']
    assert set(folded['source_ids']) == {str(first.pk), str(second.pk)}
    assert folded['source_count'] == 2 and not folded['disputed']
    assert folded['report_count'] == 1
    assert str(other.pk) not in str(shared['lab_results'])
    documents_only = project_snapshot(snapshot, {'sections': ['labs'],
        'document_ids': [str(first_doc.pk), str(second_doc.pk)]})
    assert len(documents_only['lab_results']) == 1 and not documents_only['lab_results'][0]['disputed']
    assert {row['id'] for row in documents_only['labs']} == {str(first.pk), str(second.pk)}
    assert {row['id'] for row in documents_only['documents']} == {str(first_doc.pk), str(second_doc.pk)}
    narrow = build_snapshot(patient, {'mode': 'all', 'observation_ids': [str(first.pk), str(second.pk)]})
    assert len(narrow['lab_results']) == 1 and not narrow['lab_results'][0]['disputed']


def test_confirming_different_reports_resolves_identity_dispute(django_user_model):
    _, patient = _patient(django_user_model, 'report-conflict-different')
    report(patient, number='EARLIER', at='2026-09-16 08:30', value='2')
    report(patient, number='CURRENT', value='5')
    report(patient, number='CURRENT', at='2026-09-17 10:30', value='6')
    relation = next(item for item in report_relations(patient) if item.state == 'REVIEW')
    assert not any(row.report_identity.sampled_at.date() == date(2026, 9, 17) and not row.report_conflict
                   for row in effective_rows(patient))
    decide_relation(patient, patient.account, relation.pk, 'DIFFERENT', expected_revision=relation.revision_number,
                    rationale='对照原件确认为独立报告', operation_id='resolve-report-identity')
    trend = trend_view(patient, 'LAB_WBC', include_history=True)
    assert [point.observation.raw_value for series in trend.series for point in series.points] == ['2', '6']
    assert not trend.disputed


@pytest.mark.parametrize('action', ['SAME', 'UNDO'])
def test_association_alone_does_not_resolve_conflicting_transcriptions(django_user_model, action):
    _, patient = _patient(django_user_model, 'report-conflict-kept-' + action)
    report(patient)
    report(patient, at='2026-09-17 10:30')
    relation, = report_relations(patient)
    relation = decide_relation(patient, patient.account, relation.pk, 'SAME', expected_revision=relation.revision_number,
        rationale='先确认属于同一报告，时间另行核对', operation_id='same-with-conflict')
    if action == 'UNDO':
        decide_relation(patient, patient.account, relation.pk, 'UNDO', expected_revision=relation.revision_number,
            rationale='撤销归并，时间仍有争议', operation_id='undo-with-conflict')
    assert all(row.report_conflict for row in effective_rows(patient))
    snapshot = build_snapshot(patient, {'mode': 'all'})
    assert len(snapshot['lab_results']) == 2
    assert all(row['disputed'] for row in snapshot['lab_results'])


@pytest.mark.parametrize('different_patient', [False, True])
def test_historical_report_relations_are_applied_on_first_comparison_read(django_user_model, different_patient):
    from apps.labs.models import LabReportUnit
    from apps.processing.models import OcrBlock
    from tests.labs.test_trends import _observation
    from tests.labs.test_report_identity import page

    _, patient = _patient(django_user_model, 'historical-report-conflict-' + str(different_patient))
    for person in ('合成人甲', '合成人乙' if different_patient else '合成人甲'):
        _, row = _observation(patient, date(2026, 9, 17), '5')
        evidence = page('合成医院', '检验报告', '报告号：A100', '采样时间：2026-09-17 08:30',
                        f'姓名：{person}', '白细胞 5')
        for region in evidence.regions:
            OcrBlock.objects.create(parsing_version=row.parsing_version, document_page=row.document_page,
                text=region.text, polygon=region.polygon, confidence=region.confidence, reading_order=region.reading_order)
    assert not LabReportUnit.objects.exists()
    table = comparison_view(patient)
    assert table.report_count == (2 if different_patient else 1)
    assert table.result_count == (2 if different_patient else 1)
    assert all(row.report_conflict == different_patient for row in effective_rows(patient))
    assert LabReportUnit.objects.count() == 2


def test_trend_page_retains_disputed_sources_when_no_main_line_remains(django_user_model):
    client, patient = _patient(django_user_model, 'report-conflict-no-main-line')
    report(patient, value='5')
    report(patient, value='6')
    response = client.get('/trends/LAB_WBC/')
    assert response.status_code == 200
    assert '报告归属存在冲突' in response.content.decode()
    assert '全部采样结果与来源' in response.content.decode()
    assert not response.context['trend'].series
    assert len(response.context['trend'].disputed) == 2


def test_card_lab_selection_rechecks_identity_before_selecting_trend_points(django_user_model):
    _, patient = _patient(django_user_model, 'report-conflict-card-selection')
    _, earlier, _ = report(patient, number='EARLIER', at='2026-09-16 08:30', value='2')
    _, first, _ = report(patient, value='5')
    report(patient, value='5', person='合成人乙')
    selection = {'mode': 'all', 'lab_ids': [str(earlier.pk), str(first.pk)]}
    snapshot = build_snapshot(patient, selection)
    assert [point['value'] for series in snapshot['card']['trends'] for point in series['points']] == ['2', '5']
    assert any(item['disputed'] for item in snapshot['lab_results'])


def test_card_continuation_can_use_selected_main_report_with_a_different_indicator(django_user_model):
    from tests.labs.test_report_readmodels import continuation_pair
    from tests.labs.test_trends import _observation

    _, patient = _patient(django_user_model, 'report-card-different-main-indicator')
    _, earlier = _observation(patient, date(2026, 9, 16), '35', code='LAB_ALB', standard_name='白蛋白',
        raw_name='白蛋白', raw_unit='g/L', institution='合成医院')
    _, rows, _ = continuation_pair(patient)
    continuation = rows[1]
    continuation.standard_code = 'LAB_ALB'
    continuation.standard_name = continuation.raw_name = '白蛋白'
    continuation.raw_value = '40'
    continuation.raw_unit = 'g/L'
    continuation.save(update_fields=['standard_code', 'standard_name', 'raw_name', 'raw_value', 'raw_unit'])
    snapshot = build_snapshot(patient, {'mode': 'all', 'lab_ids': [str(row.pk) for row in (earlier, *rows)]})
    assert [point['value'] for series in snapshot['card']['trends'] for point in series['points']] == ['35', '40']
