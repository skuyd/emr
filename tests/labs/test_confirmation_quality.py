import re
from datetime import date, datetime
from decimal import Decimal

import pytest

from apps.labs.comparison import comparison_view
from apps.labs.readmodels import effective_rows
from apps.labs.reports import decide_relation, report_relations
from apps.labs.revisions import effective_observation, revise_observation
from apps.labs.trends import trend_view
from apps.labs.validation import validate_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_batch_confirmation import PATH, payload, preview
from tests.labs.test_report_relations import report
from tests.labs.test_report_readmodels import continuation_pair
from tests.labs.test_report_revision_versions import next_report_version


pytestmark = pytest.mark.django_db
PENDING_LABELS = ('标本待确认', '指标待核对', '结果待核对')


def uncertain(row, *, association=False):
    row.evidence.confidence = '.4'
    row.evidence.save(update_fields=['confidence'])
    if association:
        row.quality_issues = [{'code': 'association_conflict',
                              'fields': ['raw_name', 'standard_code', 'raw_value']}]
        row.save(update_fields=['quality_issues'])


def comparison(client, patient, row):
    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert response.status_code == 200
    cell, = [cell for item in response.context['comparison'].rows for entries in item.cells
             for cell in entries if cell.observation.pk == row.pk]
    html, = [article for article in re.findall(r'<article>(.*?)</article>',
                                              response.content.decode(), flags=re.S)
             if f'id="result-{row.pk}"' in article]
    return cell, html


def confirm_batch(client, patient):
    response = client.post(PATH, payload(patient, preview(client)))
    assert response.status_code == 200
    return response.context['result']


@pytest.mark.parametrize('association', [False, True])
def test_batch_confirmation_resolves_recognition_and_association_for_comparison(django_user_model, association):
    client, patient = _patient(django_user_model, f'confirmation-quality-{association}')
    _, row, _ = report(patient)
    uncertain(row, association=association)
    before, html = comparison(client, patient, row)
    assert not before.trend_eligible
    assert '结果待核对' in html
    if association:
        assert '指标待核对' in html

    assert confirm_batch(client, patient)['confirmed_count'] == 1

    after, html = comparison(client, patient, row)
    assert after.trend_eligible and after.plot_eligible
    assert not {'recognition_uncertain', 'association_conflict'} & {
        item['code'] for item in after.quality_issues}
    assert all(label not in html for label in PENDING_LABELS)
    row.refresh_from_db()
    assert (row.specimen, row.raw_value, row.raw_unit, row.method_raw) == (
        'BLOOD', '5', '10^9/L', '合成方法A')


@pytest.mark.parametrize('field,label', [('specimen', '缺少标本'), ('raw_unit', '缺少单位')])
def test_confirmed_missing_calculation_input_is_named_without_pending_review(django_user_model, field, label):
    client, patient = _patient(django_user_model, 'confirmation-missing-' + field)
    _, row, _ = report(patient)
    setattr(row, field, '')
    row.save(update_fields=[field])
    uncertain(row)

    assert confirm_batch(client, patient)['confirmed_count'] == 1

    cell, html = comparison(client, patient, row)
    assert cell.observation.review_state == 'CONFIRM'
    assert not cell.trend_eligible
    assert all(pending not in html for pending in PENDING_LABELS)
    assert label in html
    assert 'recognition_uncertain' not in {item['code'] for item in cell.quality_issues}
    row.refresh_from_db()
    assert getattr(row, field) == getattr(effective_observation(row), field) == ''


def test_batch_skipped_errors_and_report_conflicts_keep_pending_review(django_user_model):
    client, patient = _patient(django_user_model, 'confirmation-skipped-quality')
    _, pending, _ = report(patient, number='P')
    _, error, _ = report(patient, number='E')
    _, conflict, _ = report(patient, number='X')
    _, other_conflict, _ = report(patient, number='X', value='6')
    for row in (error, conflict, other_conflict):
        row.specimen = ''
        row.save(update_fields=['specimen'])
        uncertain(row, association=True)
    revise_observation(patient.account, error.pk, action='REPORT_ERROR', changes={}, expected_revision=0)

    result = confirm_batch(client, patient)

    assert result['confirmed_count'] == 1 and result['skipped_count'] == 3
    assert pending.revisions.filter(action='CONFIRM').count() == 1
    for row in (error, conflict, other_conflict):
        assert not row.revisions.filter(action='CONFIRM').exists()
        cell, html = comparison(client, patient, row)
        assert not cell.trend_eligible
        assert all(label in html for label in PENDING_LABELS)
    assert 'reported_error' in {item['code'] for item in comparison(client, patient, error)[0].quality_issues}
    assert 'report_identity_conflict' in {
        item['code'] for item in comparison(client, patient, conflict)[0].quality_issues}


def test_undo_confirmation_restores_original_quality_blockers(django_user_model):
    client, patient = _patient(django_user_model, 'confirmation-undo-quality')
    _, row, _ = report(patient)
    uncertain(row, association=True)
    before, _ = comparison(client, patient, row)
    original_issues = {item['code'] for item in before.quality_issues}
    assert confirm_batch(client, patient)['confirmed_count'] == 1
    assert comparison(client, patient, row)[0].trend_eligible

    response = client.post(f'/labs/observations/{row.pk}/',
                           {'action': 'UNDO', 'expected_revision': 1}, follow=True)

    assert response.status_code == 200
    cell, html = comparison(client, patient, row)
    assert cell.observation.review_state == 'AUTOMATIC'
    assert {item['code'] for item in cell.quality_issues} == original_issues
    assert not cell.trend_eligible
    assert '指标待核对' in html and '结果待核对' in html


def test_changed_reparse_cannot_inherit_confirmation_to_allow_comparison(django_user_model):
    client, patient = _patient(django_user_model, 'confirmation-reparse-quality')
    _, row, unit = report(patient)
    uncertain(row)
    assert confirm_batch(client, patient)['confirmed_count'] == 1
    assert comparison(client, patient, row)[0].trend_eligible

    current, = next_report_version(unit)
    current.observations.update(raw_value='6')
    reparsed, = current.observations.all()

    cell, html = comparison(client, patient, reparsed)
    assert cell.observation.revision_conflict
    assert not cell.trend_eligible and not cell.plot_eligible
    assert {'recognition_uncertain', 'revision_conflict'} <= {
        item['code'] for item in cell.quality_issues}
    assert '指标待核对' in html and '结果待核对' in html
    assert not reparsed.revisions.exists()


@pytest.mark.parametrize('mode', ['single', 'saved'])
def test_single_and_existing_confirmation_use_the_same_quality_contract(django_user_model, mode):
    client, patient = _patient(django_user_model, 'confirmation-' + mode)
    _, row, _ = report(patient)
    uncertain(row)
    if mode == 'single':
        response = client.post(f'/labs/observations/{row.pk}/',
                               {'action': 'CONFIRM', 'expected_revision': 0}, follow=True)
        assert response.status_code == 200
    else:
        revise_observation(patient.account, row.pk, action='CONFIRM', changes={}, expected_revision=0)
    row.refresh_from_db()

    assert 'recognition_uncertain' not in {
        item['code'] for item in validate_observation(effective_observation(row))}
    cell, html = comparison(client, patient, row)
    assert cell.trend_eligible and cell.plot_eligible
    assert all(label not in html for label in PENDING_LABELS)


def test_batch_confirmation_enables_two_date_trend_and_undo_removes_it(django_user_model):
    client, patient = _patient(django_user_model, 'confirmation-main-trend')
    _, first, _ = report(patient, number='T16', at='2026-09-16 08:30', value='4.200')
    _, second, _ = report(patient, number='T17', at='2026-09-17 08:30', value='5.0')
    for row in (first, second):
        uncertain(row)
    assert trend_view(patient, 'LAB_WBC') is None
    assert client.get('/trends/LAB_WBC/', {'patient': patient.pk}).status_code == 404

    assert confirm_batch(client, patient)['confirmed_count'] == 2

    view = trend_view(patient, 'LAB_WBC')
    series, = view.series
    assert not series.historical
    assert [(point.observation.pk, point.observation.observation_date, point.numeric_value)
            for point in series.points] == [
        (first.pk, date(2026, 9, 16), Decimal('4.200')),
        (second.pk, date(2026, 9, 17), Decimal('5.0')),
    ]
    assert [point.observation.raw_value for point in series.points] == ['4.200', '5.0']
    assert len(series.segments) == 1
    response = client.get('/trends/LAB_WBC/', {'patient': patient.pk})
    assert response.status_code == 200
    assert [point.observation.pk for point in response.context['trend'].series[0].points] == [first.pk, second.pk]
    for row, value in ((first, '4.200'), (second, '5.0')):
        row.refresh_from_db()
        assert row.raw_value == value
        event, = row.revisions.all()
        assert event.before['raw_value'] == event.after['raw_value'] == value

    response = client.post(f'/labs/observations/{second.pk}/',
                           {'action': 'UNDO', 'expected_revision': 1}, follow=True)

    assert response.status_code == 200
    assert trend_view(patient, 'LAB_WBC') is None
    assert client.get('/trends/LAB_WBC/', {'patient': patient.pk}).status_code == 404
    history = trend_view(patient, 'LAB_WBC', include_history=True)
    assert [point.observation.pk for series in history.series for point in series.points] == [first.pk]


@pytest.mark.parametrize('state', ['both_confirmed', 'unconfirmed_donor', 'missing_unit'])
def test_confirmed_continuation_reuses_existing_time_link_without_changing_relation(django_user_model, state):
    client, patient = _patient(django_user_model, 'confirmation-continuation-' + state)
    _, rows, units = continuation_pair(patient)
    for row in rows:
        row.quality_issues = [{'code': 'association_conflict', 'fields': ['raw_value']}]
        if state == 'missing_unit':
            row.raw_unit = ''
        row.save(update_fields=['quality_issues', 'raw_unit'])
    relation, = report_relations(patient)
    relation = decide_relation(patient, patient.account, relation.pk, 'SAME',
        expected_revision=relation.revision_number, rationale='核实主报告与续页属于同一报告',
        operation_id='confirmed-continuation-relation')
    same_events = relation.events.filter(action='SAME').count()
    assert relation.basis['identity_or_result_uncertain']
    before = next(row for row in effective_rows(patient, include_uncertain=True, include_invalid=True)
                  if row.pk == rows[1].pk)
    assert before.report_identity.status == 'REVIEW'
    assert before.report_identity.sampled_at is None

    if state == 'unconfirmed_donor':
        revise_observation(patient.account, rows[1].pk, action='CONFIRM', changes={}, expected_revision=0)
    else:
        assert confirm_batch(client, patient)['confirmed_count'] == 2

    continued = next(row for row in effective_rows(patient, include_uncertain=True, include_invalid=True)
                     if row.pk == rows[1].pk)
    if state == 'both_confirmed':
        assert continued.report_identity.status == 'ACCEPTED'
        assert continued.report_identity.sampled_at == datetime(2026, 9, 17, 8, 30)
        assert continued.report_identity.time_source == units[0].source_key
        cell, = [cell for item in comparison_view(patient).rows for entries in item.cells for cell in entries
                 if any(source.pk == rows[1].pk for source in cell.sources)]
        assert cell.trend_eligible
    else:
        assert continued.report_identity.status == 'REVIEW'
        assert continued.report_identity.sampled_at is None
    refreshed, = report_relations(patient)
    assert refreshed.state == 'SAME'
    assert refreshed.basis['identity_or_result_uncertain']
    assert refreshed.events.filter(action='SAME').count() == same_events
