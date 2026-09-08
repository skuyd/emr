"""Current automatic meanings and retained decisions must use the same proof."""
from copy import deepcopy

import pytest

from apps.cancer_ordering import narrative_matching
from apps.cancer_ordering.models import CancerCandidate, NarrativeSource
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current
from tests.cancer_ordering.test_services import _revise
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db
CASES = [('assertion', 'NEGATED', '主诉：未见肺癌。'),
         ('subject', 'OTHER_PERSON', '主诉：父亲患肺癌。')]
CORRECTION = {'label': '肺癌', 'profile': 'LUNG', 'assertion': 'AFFIRMED', 'subject': 'CURRENT_PRIMARY'}


def prior_generation(model, monkeypatch, key, text, name, action=None):
    """Only the prior rule is synthetic; current rules and ORM actions are real."""
    _, patient = _patient(model, name)
    _, version = parsed_facts(patient, [text], document_type='UNKNOWN')
    actual = narrative_matching.narrative_candidates

    def old_rule(source):
        rows = deepcopy(actual(source))
        for row in rows:
            row[key] = 'AFFIRMED' if key == 'assertion' else 'CURRENT_PRIMARY'
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(narrative_matching, 'MATCHING_VERSION', 'synthetic-prior-automatic-meaning')
        patch.setattr(narrative_matching, 'narrative_candidates', old_rule)
        collect_current(patient, actor=patient.account)
        candidate = CancerCandidate.objects.get(patient=patient)
        assert resolve_ordering(patient)['profile'] == 'LUNG'
        if action == 'CORRECT_REVOKE_EXCLUDE':
            _revise(patient, candidate_rows(patient)[0], 'CORRECT', checked_original=True,
                    changes=CORRECTION, reason='合成原件独立转录')
            _revise(patient, candidate_rows(patient)[0], 'REVOKE')
            _revise(patient, candidate_rows(patient)[0], 'EXCLUDE')
        elif action:
            _revise(patient, candidate_rows(patient)[0], action, checked_original=action == 'CONFIRM')
        recorded = deepcopy(candidate_rows(patient)[0]['recorded_state'])
        history = list(candidate.revisions.values())
        original = deepcopy(candidate.original_data)
    collect_current(patient, actor=patient.account)
    assert NarrativeSource.objects.filter(parsing_version=version).count() == 2
    assert CancerCandidate.objects.get(patient=patient).pk == candidate.pk
    return patient, candidate, original, recorded, history


@pytest.mark.parametrize('key,current,text', CASES)
@pytest.mark.parametrize('action', [None, 'CONFIRM', 'EXCLUDE', 'DEFER'])
def test_current_generation_confirmation_records_current_automatic_content_without_rewriting_history(
        django_user_model, monkeypatch, key, current, text, action):
    patient, candidate, original, recorded, history = prior_generation(django_user_model, monkeypatch, key, text,
        'narrative-current-meaning-' + key + str(action), action)
    row, = candidate_rows(patient)
    assert row['source_valid'] and row['source_changed'] and not row['eligible_for_auto']
    assert row['recorded_state'] == recorded
    assert row['recorded_status'] == recorded['status']
    assert row['content'][key] == current
    assert row['content'] != row['recorded_state']['content']
    revision = _revise(patient, row, 'CONFIRM', checked_original=True)
    after, = candidate_rows(patient)
    assert after['content'][key] == revision.after['content'][key] == current
    assert revision.before['content'] == recorded['content']
    assert not after['manual_correction'] and not after['eligible_for_auto']
    assert after['status'] == 'CONFIRMED' and not after['source_changed']
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    candidate.refresh_from_db()
    assert candidate.original_data == original
    assert list(candidate.revisions.filter(sequence__lte=len(history)).values()) == history


@pytest.mark.parametrize('key,current,text', CASES)
def test_undo_current_confirmation_cannot_rebind_old_affirmation_to_new_proof(
        django_user_model, monkeypatch, key, current, text):
    patient, candidate, original, recorded, history = prior_generation(django_user_model, monkeypatch, key, text,
        'narrative-current-undo-' + key, 'CONFIRM')
    _revise(patient, candidate_rows(patient)[0], 'CONFIRM', checked_original=True)
    _revise(patient, candidate_rows(patient)[0], 'UNDO')
    row, = candidate_rows(patient)
    assert row['recorded_state']['content'] == recorded['content']
    assert row['content'][key] == current
    assert row['source_changed'] and row['status'] == 'PENDING'
    assert not row['eligible_for_auto'] and resolve_ordering(patient)['profile'] == 'GENERAL'
    assert list(candidate.revisions.filter(sequence__lte=len(history)).values()) == history
    candidate.refresh_from_db()
    assert candidate.original_data == original


@pytest.mark.parametrize('key,current,text', CASES)
def test_explicit_current_correction_can_change_semantics_but_revocation_cannot_use_ocr(
        django_user_model, monkeypatch, key, current, text):
    patient, candidate, original, _, _ = prior_generation(django_user_model, monkeypatch, key, text,
        'narrative-current-correct-' + key)
    _revise(patient, candidate_rows(patient)[0], 'CONFIRM', checked_original=True)
    assert candidate_rows(patient)[0]['content'][key] == current
    _revise(patient, candidate_rows(patient)[0], 'CORRECT', checked_original=True,
            changes=CORRECTION, reason='对照合成原件明确更正断言及对象')
    corrected, = candidate_rows(patient)
    assert corrected['manual_correction'] and resolve_ordering(patient)['profile'] == 'LUNG'
    _revise(patient, corrected, 'REVOKE')
    _revise(patient, candidate_rows(patient)[0], 'EXCLUDE')
    _revise(patient, candidate_rows(patient)[0], 'UNDO')
    row, = candidate_rows(patient)
    assert row['manual_correction'] and row['content']['assertion'] == 'AFFIRMED'
    assert row['status'] == 'PENDING' and not row['eligible_for_auto']
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    candidate.refresh_from_db()
    assert candidate.original_data == original


@pytest.mark.parametrize('key,current,text', CASES)
def test_prior_manual_revoke_undo_barrier_survives_new_automatic_generation(
        django_user_model, monkeypatch, key, current, text):
    patient, candidate, original, recorded, history = prior_generation(django_user_model, monkeypatch, key, text,
        'narrative-prior-manual-' + key, 'CORRECT_REVOKE_EXCLUDE')
    row, = candidate_rows(patient)
    assert row['recorded_state'] == recorded and row['manual_correction']
    assert row['content']['assertion'] == 'AFFIRMED' and row['content']['subject'] == 'CURRENT_PRIMARY'
    assert row['source_changed'] and not row['eligible_for_auto']
    _revise(patient, row, 'UNDO')
    row, = candidate_rows(patient)
    assert row['manual_correction'] and row['source_changed'] and row['status'] == 'PENDING'
    assert not row['eligible_for_auto'] and resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, row, 'CORRECT', checked_original=True, changes=CORRECTION, reason='当前原件重新明确核对')
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    candidate.refresh_from_db()
    assert candidate.original_data == original
    assert list(candidate.revisions.filter(sequence__lte=len(history)).values()) == history
