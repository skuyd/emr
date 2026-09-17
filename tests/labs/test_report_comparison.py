from datetime import date

import pytest

from apps.labs.comparison import comparison_view
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_report_relations import report
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_same_day_hospital_column_contains_all_reports_and_folded_sources(django_user_model):
    _, patient = _patient(django_user_model, 'report-columns')
    first, _, _ = report(patient, number='A1', value='5.0')
    second, _, _ = report(patient, number='B2', value='5.00', at='2026-09-17 10:30')
    view = comparison_view(patient)
    assert len(view.columns) == 1
    assert {document.pk for document in view.columns[0].documents} == {first.pk, second.pk}
    assert view.report_count == 2 and view.image_count == 2 and view.result_count == 1
    assert len(view.rows[0].cells[0]) == 1
    assert len(view.rows[0].cells[0][0].sources) == 2


def test_same_report_rephotograph_counts_one_report_two_originals(django_user_model):
    _, patient = _patient(django_user_model, 'report-count')
    report(patient)
    report(patient)
    view = comparison_view(patient)
    assert view.report_count == 1 and view.image_count == 2 and view.result_count == 1


def test_overlapping_report_sources_keep_each_unique_indicator_and_every_original(django_user_model):
    from copy import copy
    from uuid import uuid4
    from apps.exports.content import build_snapshot

    _, patient = _patient(django_user_model, 'report-partial-overlap')
    originals = [report(patient) for _ in range(2)]
    for (_, overlapping, _), (code, name, value, unit) in zip(originals, (
            ('LAB_ALB', '白蛋白', '35', 'g/L'), ('LAB_RBC', '红细胞', '4.2', '10^12/L'))):
        evidence = copy(overlapping.evidence)
        evidence.pk = uuid4()
        evidence.source_text = f'{name} {value} {unit}'
        evidence.save(force_insert=True)
        unique = copy(overlapping)
        unique.pk, unique.evidence = uuid4(), evidence
        unique.standard_code, unique.standard_name, unique.raw_name = code, name, name
        unique.raw_value, unique.raw_unit = value, unit
        unique.reading_order = 2
        unique.save(force_insert=True)
    view = comparison_view(patient)
    assert (view.report_count, view.image_count, view.result_count) == (1, 2, 3)
    assert {row.standard_code for row in view.rows} == {'LAB_WBC', 'LAB_ALB', 'LAB_RBC'}
    assert sum(len(cell.sources) for row in view.rows for column in row.cells for cell in column) == 4
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert len(snapshot['labs']) == 4 and len(snapshot['lab_results']) == 3
    assert {result['standard_code']: result['source_count'] for result in snapshot['lab_results']} == {
        'LAB_WBC': 2, 'LAB_ALB': 1, 'LAB_RBC': 1,
    }
    assert all(document.pages.count() == 1 for document, _, _ in originals)


def test_historical_missing_sampling_clock_is_only_in_pending_sources(django_user_model):
    _, patient = _patient(django_user_model, 'report-ineligible')
    document, row = _observation(patient, date(2026, 9, 17), '5', sampling_time=None)
    view = comparison_view(patient)
    assert not view.rows
    assert [item.pk for item in view.pending_sources] == [row.pk]
    assert document.pages.count() == 1


def test_unknown_institutions_do_not_share_a_column(django_user_model):
    _, patient = _patient(django_user_model, 'report-missing-hospital')
    for _ in range(2):
        _, _, unit = report(patient)
        unit.automatic['institution'] = ''
        unit.automatic['identity_reliable'] = False
        unit.save(update_fields=['automatic'])
    view = comparison_view(patient)
    assert len(view.columns) == 2
    assert view.result_count == 2
