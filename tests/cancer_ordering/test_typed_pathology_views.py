"""Actual typed source navigation and separate candidate/parent review."""
import pytest
from django.urls import reverse

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current, revise_candidate, OrderingConflict
from tests.cancer_ordering.test_services import _revise
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.facts.pathology_factories import add_field, report_fixture, review


pytestmark = pytest.mark.django_db


def test_private_detail_identifies_typed_histology_and_real_parent_field(django_user_model):
    client, patient, _, _, field, _ = typed_fixture(django_user_model)
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    response = client.get(reverse('cancer_ordering:detail', args=[candidate.pk]), {'patient': patient.pk})
    assert response.status_code == 200
    body = response.content.decode()
    assert '组织学字段' in body
    assert reverse('facts:detail', args=[field.pk]) in body
    assert '不会确认标本信息或组织学字段' in body
    assert candidate.source_report_id == field.clinical_report_id
    field.refresh_from_db()
    assert field.revision_number == 0


def test_candidate_confirmation_does_not_bypass_an_unreviewed_specimen(django_user_model):
    _, patient, _, _, field, anchor = typed_fixture(django_user_model, confirm_anchor=False)
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    row = next(row for row in candidate_rows(patient) if row['id'] == str(candidate.pk))
    with pytest.raises(OrderingConflict):
        _revise(patient, row, 'CONFIRM', checked_original=True)
    assert not candidate.revisions.exists()
    field.refresh_from_db()
    anchor.refresh_from_db()
    assert field.revision_number == anchor.revision_number == 0
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_genuine_manual_field_is_a_transcription_and_requires_candidate_review(django_user_model):
    _, patient, _, report = report_fixture(django_user_model, 'cancer-manual-histology')
    anchor = add_field(patient, report, 'specimen.identity', 'specimen:a', {'label': '标本甲', 'raw': '标本甲'}, {})
    review(patient, anchor)
    field = add_field(patient, report, 'specimen.histology', 'specimen:a',
        {'text': '肺癌', 'assertion': 'SOURCE_TEXT_ONLY_NOT_DIAGNOSED'}, {'SPECIMEN': anchor})
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    row = next(row for row in candidate_rows(patient) if row['id'] == str(candidate.pk))
    assert row['binding_kind'] == 'TRANSCRIBED' and not row['eligible_for_auto']
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    before = field.revision_number
    _revise(patient, row, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    field.refresh_from_db()
    assert field.revision_number == before
