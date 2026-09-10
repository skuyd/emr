from copy import deepcopy
from decimal import Decimal
import hashlib

from django.core.exceptions import PermissionDenied, ValidationError
import pytest

from apps.cancer_ordering.models import CancerCandidate, CandidateRevision, CollectionRun, DisplaySelection, SelectionRevision
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current, revise_candidate, select_ordering, undo_selection, OrderingConflict
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientMembership
from apps.processing.models import OcrBlock, SourceEvidence
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def _collect(patient, texts=('出院诊断：肺癌。',)):
    document, version = parsed_facts(patient, list(texts))
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.filter(patient=patient).exists()
    return document, version


def _row(patient, profile='LUNG'):
    rows = [row for row in candidate_rows(patient) if row['content']['profile'] == profile]
    assert rows
    return rows[-1]


def _revise(patient, row, action, **kwargs):
    return revise_candidate(patient, row['id'], actor=patient.account, action=action,
                            expected_revision=row['revision_number'], expected_source=row['current_source_token'], **kwargs)


def _select(patient, mode, **kwargs):
    state = resolve_ordering(patient)
    return select_ordering(patient, actor=patient.account, mode=mode, expected_revision=state['revision_number'],
                           expected_fingerprint=state['fingerprint'], **kwargs)


def test_current_literal_is_persisted_once_and_auto_ordering_does_not_confirm_any_fact(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-collect-once')
    _, version = _collect(patient)
    original = CancerCandidate.objects.get(patient=patient)
    before = deepcopy(original.original_data)
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.filter(patient=patient).count() == 1
    assert CollectionRun.objects.filter(patient=patient).count() == 1
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    assert resolve_ordering(patient)['mode'] == 'AUTO'
    assert not DisplaySelection.objects.filter(patient=patient).exists()
    assert not CandidateRevision.objects.exists() and not Fact.objects.get(parsing_version=version).revisions.exists()
    original.refresh_from_db()
    assert original.original_data == before


def test_successful_empty_collection_is_distinct_from_missing_coverage(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-empty-collection')
    parsed_facts(patient, ['合成出院资料，未列诊断栏目。'])
    assert resolve_ordering(patient)['complete'] is False
    collect_current(patient, actor=patient.account)
    run = CollectionRun.objects.filter(patient=patient).first()
    assert run is not None and run.status == 'COMPLETE' and run.candidate_count == 0
    assert resolve_ordering(patient)['complete'] is True
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_new_published_fact_invalidates_auto_before_any_candidate_is_materialized(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-uncollected')
    _collect(patient)
    before = resolve_ordering(patient)
    parsed_facts(patient, ['出院诊断：胰腺癌。'])
    missing = resolve_ordering(patient)
    assert missing['profile'] == 'GENERAL' and missing['complete'] is False
    assert missing['fingerprint'] != before['fingerprint']
    assert CancerCandidate.objects.filter(patient=patient).count() == 1
    collect_current(patient, actor=patient.account)
    conflict = resolve_ordering(patient)
    assert conflict['complete'] is True and conflict['reason'] == 'reported_diagnoses_differ'
    assert conflict['profile'] == 'GENERAL'


@pytest.mark.parametrize('low', [Decimal('.9499'), None])
def test_low_or_missing_source_confidence_requires_independent_candidate_review(django_user_model, low):
    _, patient = _patient(django_user_model, 'cancer-confidence-' + str(low))
    _, version = parsed_facts(patient, ['出院诊断：', '肺癌。'])
    SourceEvidence.objects.filter(parsing_version=version).update(confidence=low)
    if low is not None:
        OcrBlock.objects.filter(parsing_version=version, reading_order=1).update(confidence=low)
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.filter(patient=patient).count() == 1
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, _row(patient), 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    assert Fact.objects.get(parsing_version=version).revision_number == 0


def test_parent_exclude_undo_and_recollect_do_not_resurrect_candidate_confirmation(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-parent-undo')
    _, version = _collect(patient)
    _revise(patient, _row(patient), 'CONFIRM', checked_original=True)
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    revise_fact(patient, fact.pk, actor=patient.account, action='UNDO', expected_revision=1)
    collect_current(patient, actor=patient.account)
    restored = _row(patient)
    assert restored['status'] == 'PENDING' and restored['source_changed']
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, restored, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'


def test_fact_text_correction_adds_new_occurrence_and_preserves_original_candidate(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-parent-correct')
    _, version = _collect(patient)
    original = CancerCandidate.objects.get(patient=patient)
    original_data = deepcopy(original.original_data)
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, actor=patient.account, action='CORRECT', expected_revision=0, checked_original=True,
                changes={'text': '出院诊断：胰腺癌。'})
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.filter(patient=patient).count() == 2
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, _row(patient, 'PANCREAS'), 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'PANCREAS'
    original.refresh_from_db()
    assert original.original_data == original_data and fact.raw_text == '出院诊断：肺癌。'


def test_reported_negation_survives_confirmation_and_requires_explicit_correction(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-explicit-correction')
    _collect(patient, ['出院诊断：未见肺癌。'])
    original = CancerCandidate.objects.get(patient=patient)
    original_data = deepcopy(original.original_data)
    _revise(patient, _row(patient), 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, _row(patient), 'CORRECT', checked_original=True, reason='对照合成原件重新转录断言',
             changes={'label': '肺癌', 'profile': 'LUNG', 'assertion': 'AFFIRMED', 'subject': 'CURRENT_PRIMARY'})
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    original.refresh_from_db()
    assert original.original_data == original_data
    assert _row(patient)['manual_correction'] is True


def test_exclusion_and_undo_change_the_current_conflict_without_dropping_history(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-candidate-undo')
    _collect(patient, ['出院诊断：肺癌；胰腺癌。'])
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, _row(patient, 'PANCREAS'), 'EXCLUDE')
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    row = _row(patient, 'PANCREAS')
    _revise(patient, row, 'UNDO')
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    with pytest.raises(OrderingConflict):
        _revise(patient, row, 'UNDO')
    assert CandidateRevision.objects.count() == 2


def test_explicit_modes_override_new_reports_and_restore_auto_by_an_event(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-selection-modes')
    _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
    assert DisplaySelection.objects.filter(patient=patient).count() == 1
    assert resolve_ordering(patient)['profile'] == 'PANCREAS'
    _collect(patient)
    assert resolve_ordering(patient)['profile'] == 'PANCREAS'
    _select(patient, 'GENERAL')
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _select(patient, 'AUTO')
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    assert SelectionRevision.objects.count() == 3


def test_explicit_candidate_selection_keeps_its_choice_but_falls_back_on_source_change(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-selection-source')
    _, version = _collect(patient)
    _select(patient, 'CANDIDATE', candidate_id=_row(patient)['id'])
    _collect(patient, ['出院诊断：胰腺癌。'])
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=0, checked_original=True)
    state = resolve_ordering(patient)
    assert state['mode'] == 'CANDIDATE' and state['profile'] == 'GENERAL'
    assert state['reason'] == 'selection_source_changed'
    assert SelectionRevision.objects.count() == 1


def test_selection_undo_is_append_only_and_old_form_cannot_repeat_it(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-selection-undo')
    _select(patient, 'MANUAL_PROFILE', profile='LUNG')
    _select(patient, 'GENERAL')
    state = resolve_ordering(patient)
    undo_selection(patient, actor=patient.account, expected_revision=state['revision_number'], expected_fingerprint=state['fingerprint'])
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    with pytest.raises(OrderingConflict):
        undo_selection(patient, actor=patient.account, expected_revision=state['revision_number'], expected_fingerprint=state['fingerprint'])
    assert SelectionRevision.objects.count() == 3


def test_actual_reviewer_purge_clears_current_confirmation_and_preserves_anonymous_history(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-reviewer-purge')
    _collect(patient)
    actor = django_user_model.objects.create(phone_hash=hashlib.sha256(b'cancer-reviewer').hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    row = _row(patient)
    revise_candidate(patient, row['id'], actor=actor, action='CONFIRM', expected_revision=0,
                      expected_source=row['current_source_token'], checked_original=True)
    actor.delete()
    current = _row(patient)
    assert current['status'] == 'PENDING' and current['source_changed']
    assert CandidateRevision.objects.get().author_id is None
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, current, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'


def test_viewer_and_missing_actor_cannot_create_collections_or_choices(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-write-owner')
    _, viewer_space = _patient(django_user_model, 'cancer-write-viewer')
    PatientMembership.objects.create(patient=patient, account=viewer_space.account, role='VIEWER')
    for actor in (viewer_space.account, None):
        with pytest.raises(PermissionDenied):
            collect_current(patient, actor=actor)
        with pytest.raises(PermissionDenied):
            select_ordering(patient, actor=actor, mode='MANUAL_PROFILE', profile='LUNG', expected_revision=0, expected_fingerprint='')
    assert not CollectionRun.objects.exists() and not DisplaySelection.objects.exists()


def test_stale_source_cross_patient_and_immutable_original_are_rejected(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-write-scope')
    _, other = _patient(django_user_model, 'cancer-write-other')
    _, version = _collect(patient)
    row = _row(patient)
    with pytest.raises(PermissionDenied):
        revise_candidate(other, row['id'], actor=other.account, action='EXCLUDE', expected_revision=0,
                          expected_source=row['current_source_token'])
    revise_fact(patient, Fact.objects.get(parsing_version=version).pk, actor=patient.account,
                action='CONFIRM', expected_revision=0, checked_original=True)
    with pytest.raises(OrderingConflict):
        _revise(patient, row, 'CONFIRM', checked_original=True)
    original = CancerCandidate.objects.get(pk=row['id'])
    original.original_data = {'label': 'forged'}
    with pytest.raises(ValidationError):
        original.save()
    assert not CandidateRevision.objects.exists()
