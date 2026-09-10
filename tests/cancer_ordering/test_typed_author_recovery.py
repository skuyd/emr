import hashlib
import pytest
from apps.cancer_ordering.sources import SourceContext
from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.services import collect_current
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.patients.models import PatientMembership
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.cancer_ordering.test_services import _revise
from tests.facts.pathology_factories import review

pytestmark = pytest.mark.django_db

@pytest.mark.parametrize('change', ['deactivate', 'delete'])
def test_current_explicit_reviews_can_recover_after_historical_anchor_author_change(django_user_model, change):
    _, patient, _, _, field, anchor = typed_fixture(django_user_model, name='root-history-' + change)
    actor = django_user_model.objects.create(phone_hash=hashlib.sha256(('root-history-editor-' + change).encode()).hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    review(patient, anchor, 'DEFER', actor=actor)
    review(patient, anchor, 'CONFIRM')
    collect_current(patient, actor=patient.account)
    assert SourceContext().fact(field.pk).source_valid
    if change == 'delete':
        actor.delete()
    else:
        actor.is_active = False
        actor.save(update_fields=['is_active'])
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    review(patient, anchor, 'CONFIRM')
    review(patient, field, 'CONFIRM')
    source = SourceContext().fact(field.pk)
    assert source.display_context['context_state'] == 'RESOLVED'
    assert source.source_valid, 'Current explicit source reviews cannot recover while old inactive author remains'
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    row = next(row for row in candidate_rows(patient) if row['id'] == str(candidate.pk))
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, row, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'


def editor(user_model, patient, name):
    actor = user_model.objects.create(phone_hash=hashlib.sha256(name.encode()).hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    return actor


@pytest.mark.parametrize('parent', ['field', 'anchor'])
def test_current_inactive_reviewer_remains_an_invalid_source(django_user_model, parent):
    _, patient, _, _, field, anchor = typed_fixture(django_user_model, name='current-author-' + parent)
    actor = editor(django_user_model, patient, 'current-editor-' + parent)
    review(patient, field if parent == 'field' else anchor, 'CONFIRM', actor=actor)
    actor.is_active = False
    actor.save(update_fields=['is_active'])
    assert not SourceContext().fact(field.pk).source_valid
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


@pytest.mark.parametrize('parent', ['field', 'anchor'])
def test_inactive_manual_creator_cannot_be_replaced_by_active_review(django_user_model, parent):
    from tests.facts.pathology_factories import report_fixture, add_field
    _, patient, _, report = report_fixture(django_user_model, 'manual-creator-' + parent)
    actor = editor(django_user_model, patient, 'manual-editor-' + parent)
    anchor = add_field(patient, report, 'specimen.identity', 'specimen:a',
        {'label': '标本甲', 'raw': '标本甲'}, {}, actor=actor if parent == 'anchor' else None)
    review(patient, anchor)
    field = add_field(patient, report, 'specimen.histology', 'specimen:a',
        {'text': '肺癌', 'assertion': 'SOURCE_TEXT_ONLY_NOT_DIAGNOSED'}, {'SPECIMEN': anchor},
        actor=actor if parent == 'field' else None)
    review(patient, field)
    actor.is_active = False
    actor.save(update_fields=['is_active'])
    assert not SourceContext().fact(field.pk).source_valid


@pytest.mark.parametrize('route', ['typed', 'excerpt'])
def test_historical_anchor_author_cannot_regain_auto_via_collection_or_replacement(django_user_model, route):
    from tests.cancer_ordering.test_typed_occurrence_history import replacement
    _, patient, _, _, field, anchor = typed_fixture(django_user_model, name='history-replace-' + route,
        text='肺癌。', heading='病理诊断' if route == 'excerpt' else '组织学诊断')
    actor = editor(django_user_model, patient, 'history-replace-editor-' + route)
    review(patient, anchor, 'DEFER', actor=actor)
    review(patient, anchor)
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    initial_confidence = SourceContext().fact(field.pk).confidence_values
    actor.is_active = False
    actor.save(update_fields=['is_active'])
    review(patient, anchor)
    field = replacement(field)
    source = SourceContext().fact(field.pk)
    assert source.source_valid
    assert source.confidence_values == initial_confidence
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    candidates = [member.candidate for run in collect_current(patient, actor=patient.account) for member in run.members.all()]
    assert len(candidates) == 1
    row = next(row for row in candidate_rows(patient) if row['id'] == str(candidates[0].pk))
    _revise(patient, row, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
