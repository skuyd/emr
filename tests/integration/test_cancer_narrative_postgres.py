"""Independent connections and normal COMMIT at narrative dependency boundaries."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from queue import Queue
import uuid

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
import pytest

from apps.cancer_ordering.models import CancerCandidate, CandidateRevision, CollectionRun, NarrativeSource
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current, revise_candidate, OrderingConflict
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.access import authorize_patient, Capability, change_membership
from apps.patients.models import PatientMembership
from apps.processing.models import ParsingVersion
from apps.processing.runner import run_processing, ExecutionState
from apps.documents.models import ProcessingRun
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.cancer_ordering.test_narrative_dependencies import narrative_case, current_row
from tests.cancer_ordering.test_pipeline import _page
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.integration.test_cancer_ordering_postgres import _fresh_state
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call
from tests.processing.test_pipeline import _document_and_run, _pipeline, _png_bytes, _Store


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated narrative PostgreSQL test database')


@pytest.mark.parametrize('change', ['parent', 'membership', 'reparse'])
def test_waiting_narrative_confirmation_rechecks_committed_dependency(django_user_model, change):
    patient, document, version, parent, candidate = narrative_case(django_user_model, 'narrative-pg-' + change)
    actor = django_user_model.objects.create(phone_hash=hashlib.sha256(('narrative-pg-editor-' + change).encode()).hexdigest(), phone_encrypted='synthetic')
    membership = PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    prior = current_row(patient)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'parent':
                revise_fact(patient, parent.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
            elif change == 'membership':
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            else:
                parsed_facts(patient, ['现病史：患者诊断为肺癌，已行化疗。'], document=document, previous=version)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_candidate(patient, candidate.pk, actor=actor,
                action='CONFIRM', expected_revision=prior['revision_number'], expected_source=prior['current_source_token'],
                checked_original=True), pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(PermissionDenied if change == 'membership' else OrderingConflict):
            future.result(timeout=20)
    assert not CandidateRevision.objects.filter(candidate=candidate).exists()


def test_two_collectors_commit_one_source_generation_and_distinct_original_occurrences(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-pg-idempotent')
    parsed_facts(patient, ['主诉：肺癌；肺癌。'], document_type='UNKNOWN')
    pids = Queue()
    with ThreadPoolExecutor(max_workers=2) as pool:
        with transaction.atomic():
            authorize_patient(patient, patient.account, Capability.WRITE, lock=True)
            blocker = backend_pid()
            first = pool.submit(thread_call, lambda: collect_current(patient, actor=patient.account), pids)
            first_pid = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=first_pid, blocker_pid=blocker)
            second = pool.submit(thread_call, lambda: collect_current(patient, actor=patient.account), pids)
            second_pid = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=second_pid, blocker_pid=first_pid)
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert results[0][0].pk == results[1][0].pk
    assert CollectionRun.objects.filter(patient=patient).count() == 1
    assert NarrativeSource.objects.filter(document__patient=patient).count() == 2
    assert CancerCandidate.objects.filter(patient=patient).count() == 2
    assert resolve_ordering(patient)['complete'] and resolve_ordering(patient)['profile'] == 'LUNG'


def test_parent_confirmation_and_recollect_keep_exclusion_across_real_commit(django_user_model):
    patient, _, _, parent, candidate = narrative_case(django_user_model, 'narrative-pg-barrier')
    row = current_row(patient)
    revise_candidate(patient, candidate.pk, actor=patient.account, action='EXCLUDE', expected_revision=0,
                     expected_source=row['current_source_token'])
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_fact(patient, parent.pk, actor=patient.account, action='CONFIRM', expected_revision=0, checked_original=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: collect_current(patient, actor=patient.account), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=waiter, blocker_pid=blocker)
        assert future.result(timeout=20)[0].status == 'COMPLETE'
    assert CancerCandidate.objects.get(patient=patient).pk == candidate.pk
    assert candidate.revisions.count() == 1
    assert not current_row(patient)['eligible_for_auto'] and resolve_ordering(patient)['profile'] == 'GENERAL'


def test_historical_author_purge_commits_after_collection_without_author_fk_deadlock(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion
    from apps.accounts.models import Account
    patient, _, _, parent, candidate = narrative_case(django_user_model, 'narrative-pg-purge')
    author = django_user_model.objects.create(phone_hash=hashlib.sha256(b'narrative-pg-historical-author').hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=author, role='EDITOR')
    revise_fact(patient, parent.pk, actor=author, action='DEFER', expected_revision=0)
    revise_fact(patient, parent.pk, actor=patient.account, action='CONFIRM', expected_revision=1, checked_original=True)
    collect_current(patient, actor=patient.account)
    prior = current_row(patient)
    revise_candidate(patient, candidate.pk, actor=patient.account, action='CONFIRM', expected_revision=0,
                     expected_source=prior['current_source_token'], checked_original=True)
    before = resolve_ordering(patient)
    author_id, pids = author.pk, Queue()
    def purge():
        job = request_account_deletion(author_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        purge_account_deletion(job.pk)
        return Account.objects.filter(pk=author_id).exists()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(patient, patient.account, Capability.WRITE, lock=True)
            blocker = backend_pid()
            future = pool.submit(thread_call, purge, pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=waiter, blocker_pid=blocker)
            collect_current(patient, actor=patient.account)
        assert future.result(timeout=20) is False
    after = resolve_ordering(patient)
    assert after['fingerprint'] != before['fingerprint'] and after['profile'] == 'GENERAL'
    assert parent.revisions.order_by('sequence').first().author_id is None
    assert str(author_id) not in str(current_row(patient))


@pytest.mark.parametrize('decision', [None, 'EXCLUDE', 'CORRECT'])
def test_real_reparse_narrative_collection_survives_publication_with_inherited_parent(django_user_model, decision):
    document, run = _document_and_run(django_user_model)
    pipeline = _pipeline(_Store(_png_bytes()), _page('现病史：患者诊断为肺癌，已行化疗。'))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    first = ParsingVersion.objects.get(processing_run=run)
    parent = Fact.objects.get(parsing_version=first)
    if decision:
        revise_fact(document.patient, parent.pk, actor=document.patient.account, action=decision,
            expected_revision=0, checked_original=decision == 'CORRECT',
            changes={'text': '现病史：患者诊断为胰腺癌，已行化疗。'} if decision == 'CORRECT' else None)
    next_run = ProcessingRun.objects.create(document=document, parser_version='narrative-test-v2',
        task_type='reparse', attempt_number=2, idempotency_key=str(uuid.uuid4()))
    assert run_processing(next_run.pk, pipeline).state == ExecutionState.SUCCEEDED
    current = ParsingVersion.objects.get(processing_run=next_run)
    assert current.previous_version_id == first.pk
    state = resolve_ordering(document.patient)
    assert state['complete'] and state['profile'] == ('LUNG' if decision is None else 'GENERAL')
    assert CollectionRun.objects.get(parsing_version=current).status == 'COMPLETE'
    assert CancerCandidate.objects.filter(source_narrative__parsing_version=current).count() == 1
