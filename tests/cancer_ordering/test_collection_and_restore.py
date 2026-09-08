import hashlib

from django.db import transaction
import pytest

from apps.cancer_ordering import matching
from apps.cancer_ordering.models import CancerCandidate, CollectionRun
from apps.cancer_ordering.readmodels import resolve_ordering, candidate_rows
from apps.cancer_ordering.services import collect_current, collect_scope, revise_candidate
from apps.cancer_ordering.sources import SourceContext
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientMembership
from apps.processing.models import ParsingVersion
from tests.cancer_ordering.test_services import _collect, _row, _revise
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def test_partial_collector_failure_rolls_back_candidates_retains_failed_attempt_and_retries(django_user_model, monkeypatch):
    _, patient = _patient(django_user_model, 'cancer-collection-failure')
    _, version = parsed_facts(patient, ['出院诊断：肺癌。', '病理诊断：胰腺癌。'])
    original = matching.literal_candidates
    def failing(text, category):
        if category == 'PATHOLOGY':
            raise ValueError('Synthetic source detail that must not become a persisted error')
        return original(text, category)
    with monkeypatch.context() as patch:
        patch.setattr(matching, 'literal_candidates', failing)
        collect_current(patient, actor=patient.account)
    run = CollectionRun.objects.get(patient=patient)
    assert run.status == 'FAILED' and run.error_code == 'candidate_collection_failed'
    assert not CancerCandidate.objects.exists() and Fact.objects.filter(parsing_version=version).count() == 2
    assert resolve_ordering(patient)['complete'] is False
    collect_current(patient, actor=patient.account)
    assert list(CollectionRun.objects.order_by('sequence').values_list('status', flat=True)) == ['FAILED', 'COMPLETE']
    assert CancerCandidate.objects.count() == 2 and resolve_ordering(patient)['complete'] is True


def test_ready_collection_remains_complete_and_automatically_useful_after_real_activation(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-collection-publish')
    _, version = parsed_facts(patient, ['出院诊断：肺癌。'])
    ParsingVersion.objects.filter(pk=version.pk).update(active=False, status='READY', published_at=None)
    scope, = SourceContext().scopes(patient, version=version.pk)
    with transaction.atomic():
        run = collect_scope(scope, patient=patient, author=None)
    assert run.status == 'COMPLETE' and run.candidate_count == 1
    ParsingVersion.objects.activate(version.pk)
    state = resolve_ordering(patient)
    assert state['complete'] is True and state['profile'] == 'LUNG'
    assert CollectionRun.objects.count() == 1 and candidate_rows(patient)[0]['status'] == 'PENDING'


def test_outdated_collection_rule_is_visible_and_explicit_recollection_uses_new_occurrences(django_user_model, monkeypatch):
    _, patient = _patient(django_user_model, 'cancer-collection-rule')
    _collect(patient)
    original = CancerCandidate.objects.get()
    before = resolve_ordering(patient)
    monkeypatch.setattr(matching, 'MATCHING_VERSION', 'synthetic-next-rule')
    stale = resolve_ordering(patient)
    assert stale['profile'] == 'GENERAL' and not stale['complete'] and stale['fingerprint'] != before['fingerprint']
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    assert CancerCandidate.objects.count() == 2
    original.refresh_from_db()
    assert original.rule_version != 'synthetic-next-rule'


def test_missing_collection_member_is_partial_coverage_and_cannot_keep_auto(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-collection-partial')
    _collect(patient, ['出院诊断：肺癌；肺癌。'])
    assert CancerCandidate.objects.count() == 2
    run = CollectionRun.objects.get()
    run.members.order_by('ordinal').last().delete()
    state = resolve_ordering(patient)
    assert state['profile'] == 'GENERAL' and state['complete'] is False
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    assert CancerCandidate.objects.count() == 2 and CollectionRun.objects.count() == 2


def test_undo_cannot_reconfirm_using_a_now_purged_original_reviewer(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-undo-purged')
    _collect(patient)
    actor = django_user_model.objects.create(phone_hash=hashlib.sha256(b'cancer-undo-author').hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    row = _row(patient)
    revise_candidate(patient, row['id'], actor=actor, action='CONFIRM', expected_revision=0,
                      expected_source=row['current_source_token'], checked_original=True)
    _revise(patient, _row(patient), 'EXCLUDE')
    actor.delete()
    _revise(patient, _row(patient), 'UNDO')
    current = _row(patient)
    assert current['status'] == 'PENDING'
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
    _revise(patient, current, 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'


def test_revoke_after_parent_change_does_not_acknowledge_new_source_without_original_review(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-revoke-source')
    _, version = _collect(patient)
    _revise(patient, _row(patient), 'CONFIRM', checked_original=True)
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=0, checked_original=True)
    collect_current(patient, actor=patient.account)
    _revise(patient, _row(patient), 'REVOKE')
    assert _row(patient)['status'] == 'PENDING'
    assert resolve_ordering(patient)['profile'] == 'GENERAL'
