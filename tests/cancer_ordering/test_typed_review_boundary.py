"""Retained review remains explicit for page-only manual original sources."""
import pytest

from apps.cancer_ordering.models import CancerCandidate, OccurrenceReview
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current
from tests.cancer_ordering.test_services import _revise
from tests.cancer_ordering.test_typed_occurrence_history import replacement
from tests.facts.pathology_factories import add_field, report_fixture, review

pytestmark = pytest.mark.django_db


def test_manual_same_page_replacement_preserves_exclusion_until_explicit_new_review(django_user_model):
    _, patient, _, report = report_fixture(django_user_model, 'manual-history-boundary')
    anchor = add_field(patient, report, 'specimen.identity', 'specimen:a', {'label': '标本甲', 'raw': '标本甲'}, {})
    review(patient, anchor)
    field = add_field(patient, report, 'specimen.histology', 'specimen:a',
        {'text': '肺癌', 'assertion': 'SOURCE_TEXT_ONLY_NOT_DIAGNOSED'}, {'SPECIMEN': anchor})
    collect_current(patient, actor=patient.account)
    original = CancerCandidate.objects.get(source_fact=field)
    row = next(row for row in candidate_rows(patient) if row['id'] == str(original.pk))
    _revise(patient, row, 'EXCLUDE')
    current = replacement(field)
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=current)
    row = next(row for row in candidate_rows(patient) if row['id'] == str(candidate.pk))
    assert row['source_valid'] and row['status'] == 'EXCLUDED' and row['source_changed']
    event = OccurrenceReview.objects.get(original_candidate_id=original.pk)
    assert event.candidate_id is None and event.parsing_version_id is None
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, row, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    assert OccurrenceReview.objects.get(pk=event.pk).after['status'] == 'EXCLUDED'
