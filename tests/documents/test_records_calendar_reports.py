from datetime import date

import pytest
from freezegun import freeze_time

from apps.documents.archive import records_context
from apps.facts.laterality import review_parent_arguments
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.processing.models import DatePrecision, DocumentSummary
from tests.facts.test_clinical_foundation import clinical_fixture
from tests.labs.helpers import _observation


pytestmark = pytest.mark.django_db


def _cells(context):
    return {cell['date']: cell for week in context['calendar_weeks'] for cell in week}


def _confirm_fields(patient, document):
    for fact in document.facts.filter(representation='FIELD'):
        revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=0,
                    expected_source=effective_fact(fact)['current_source_token'], checked_original=True,
                    **review_parent_arguments(fact))


def _set_summary_day(document, value):
    DocumentSummary.objects.filter(parsing_version__document=document, parsing_version__active=True).update(
        document_date=value, date_precision=DatePrecision.DAY,
    )


def test_multiple_clinical_report_dates_share_one_document_across_calendar_days(django_user_model):
    first = ('CT诊断报告书\n检查日期：2026-08-01\n检查项目：胸部CT\n'
             '影像表现：左肺见结节，大小12×8mm。\n诊断意见：左肺结节。\n')
    second = ('MR诊断报告书\n检查日期：2026-08-02\n检查项目：颅脑磁共振\n'
              '影像表现：右额叶见结节，大小4×3mm。\n诊断意见：右额叶结节。')
    _, patient, document, _, _ = clinical_fixture(django_user_model, texts=[first + second], name='calendar-two-reports')
    _confirm_fields(patient, document)

    first_day = records_context(patient, {'year': '2026', 'month': '8', 'date': '2026-08-01'})
    second_day = records_context(patient, {'year': '2026', 'month': '8', 'date': '2026-08-02'})

    assert _cells(first_day)[date(2026, 8, 1)]['count'] == 1
    assert _cells(second_day)[date(2026, 8, 2)]['count'] == 1
    assert first_day['month_count'] == second_day['month_count'] == 1
    assert [card.document.pk for card in first_day['day_cards']] == [document.pk]
    assert [card.document.pk for card in second_day['day_cards']] == [document.pk]


def test_conflicting_clinical_report_date_is_not_added_to_calendar(django_user_model):
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.clinical_services import add_manual_clinical_field

    _, patient, document, _, _ = clinical_fixture(django_user_model, name='calendar-date-conflict')
    _set_summary_day(document, date(2026, 8, 17))
    _confirm_fields(patient, document)
    report = document.clinical_reports.get()
    extra = add_manual_clinical_field(
        patient, actor=patient.account, report_id=report.pk, entity_key='report',
        field_key='report.exam_date', value={'value': '2026-08-18', 'precision': 'DAY'},
        fragments=[{'page_number': 1, 'raw_text': '检查日期：2026-08-18'}],
        expected_report_source=report_source_token(report),
    )
    revise_fact(patient, extra.pk, actor=patient.account, action='CONFIRM', expected_revision=0,
                expected_source=effective_fact(extra)['current_source_token'], checked_original=True,
                **review_parent_arguments(extra))

    context = records_context(patient, {'year': '2026', 'month': '8', 'date': '2026-08-18'})
    cells = _cells(context)

    assert cells[date(2026, 8, 17)]['count'] == 0
    assert cells[date(2026, 8, 18)]['count'] == 0
    assert context['month_count'] == 0
    assert document.pk in {card.document.pk for card in context['undated_cards']}


def test_corrected_clinical_report_date_replaces_stale_summary_day(django_user_model):
    _, patient, document, _, _ = clinical_fixture(django_user_model, name='calendar-date-correction')
    _set_summary_day(document, date(2026, 8, 17))
    _confirm_fields(patient, document)
    fact = document.facts.get(representation='FIELD', field_key='report.exam_date')
    revise_fact(
        patient, fact.pk, actor=patient.account, action='CORRECT', expected_revision=fact.revision_number,
        expected_source=effective_fact(fact)['current_source_token'], checked_original=True,
        changes={'value': {'value': '2026-08-18', 'precision': 'DAY'}, 'raw_value': '检查日期：2026-08-18'},
        **review_parent_arguments(fact),
    )

    corrected = records_context(patient, {'year': '2026', 'month': '8', 'date': '2026-08-18'})
    stale = records_context(patient, {'year': '2026', 'month': '8', 'date': '2026-08-17'})

    assert _cells(corrected)[date(2026, 8, 18)]['count'] == 1
    assert _cells(stale)[date(2026, 8, 17)]['count'] == 0
    assert corrected['month_count'] == stale['month_count'] == 1


def test_excluded_clinical_date_does_not_fall_back_to_stale_summary_day(django_user_model):
    _, patient, document, _, _ = clinical_fixture(django_user_model, name='calendar-excluded-date')
    _set_summary_day(document, date(2026, 8, 17))
    _confirm_fields(patient, document)
    fact = document.facts.get(representation='FIELD', field_key='report.exam_date')
    revise_fact(
        patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=fact.revision_number,
        expected_source=effective_fact(fact)['current_source_token'], **review_parent_arguments(fact),
    )

    context = records_context(patient, {'year': '2026', 'month': '8', 'date': '2026-08-17'})

    assert _cells(context)[date(2026, 8, 17)]['count'] == 0
    assert context['month_count'] == 0
    assert document.pk in {card.document.pk for card in context['undated_cards']}


@pytest.mark.parametrize(('value', 'precision'), [('2026', 'YEAR'), ('2026-08', 'MONTH'), (None, 'UNKNOWN')])
def test_corrected_partial_clinical_date_remains_undated_and_list_still_loads(django_user_model, value, precision):
    _, patient, document, _, _ = clinical_fixture(django_user_model, name='calendar-partial-date')
    _set_summary_day(document, date(2026, 8, 17))
    _confirm_fields(patient, document)
    fact = document.facts.get(representation='FIELD', field_key='report.exam_date')
    revise_fact(
        patient, fact.pk, actor=patient.account, action='CORRECT', expected_revision=fact.revision_number,
        expected_source=effective_fact(fact)['current_source_token'], checked_original=True,
        changes={'value': {'value': value, 'precision': precision}, 'raw_value': '更正后的日期'},
        **review_parent_arguments(fact),
    )

    context = records_context(patient, {'year': '2026', 'month': '8'})
    assert context['month_count'] == 0
    assert [card.document.pk for card in context['undated_cards']] == [document.pk]
    assert not any(cell['count'] for cell in _cells(context).values())
    assert records_context(patient, {'view': 'list'})['page_obj'].paginator.count == 1


def test_confirmed_lab_sampling_date_uses_sampled_day_instead_of_upload_day(django_user_model):
    from tests.documents.test_detail_viewer import _patient

    _, patient = _patient(django_user_model, 'calendar-lab-sampled-date')
    with freeze_time('2026-10-02 12:00:00'):
        document, _ = _observation(patient, date(2026, 9, 17), '5.2')
    assert document.created_at.date() == date(2026, 10, 2)

    context = records_context(patient, {'year': '2026', 'month': '9', 'date': '2026-09-17'})
    cells = _cells(context)

    assert cells[date(2026, 9, 17)]['count'] == 1
    assert context['month_count'] == 1
    assert [card.document.pk for card in context['day_cards']] == [document.pk]
    october = records_context(patient, {'year': '2026', 'month': '10', 'date': '2026-10-02'})
    assert _cells(october)[date(2026, 10, 2)]['count'] == 0
