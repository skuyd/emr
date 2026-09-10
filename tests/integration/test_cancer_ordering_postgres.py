"""Actual database commits must fence source collection and display decisions."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from queue import Queue

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
import pytest

from apps.cancer_ordering.models import CancerCandidate, CandidateRevision, CollectionRun, DisplaySelection
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current, revise_candidate, select_ordering, OrderingConflict
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact, add_manual_fact
from apps.patients.access import authorize_patient, Capability, change_membership
from apps.patients.models import PatientMembership
from apps.processing.models import ParsingVersion
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.cancer_ordering.test_services import _collect, _row
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated cancer-ordering PostgreSQL test database')


def _fresh_state(pid):
    # The second waiter connects after the first statistics read. PostgreSQL
    # caches pg_stat_activity within this monitor transaction; discard that
    # snapshot so both new connections and their current waits are observable.
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_stat_clear_snapshot()')
    return state(pid)


@pytest.mark.parametrize('change', ['parent', 'membership', 'reparse'])
def test_waiting_candidate_confirmation_rechecks_committed_parent_access_and_parse(django_user_model, change):
    _, patient, _, actor, membership = family(django_user_model, 'cancer-pg-confirm-' + change)
    document, first = _collect(patient)
    row = _row(patient)
    fact = Fact.objects.get(parsing_version=first, representation='EXCERPT')
    second = None
    if change == 'reparse':
        _, second = parsed_facts(patient, ['出院诊断：肺癌。'], document=document, previous=first)
        ParsingVersion.objects.activate(first.pk)
        row = _row(patient)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'parent':
                revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
            elif change == 'membership':
                change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
            else:
                ParsingVersion.objects.activate(second.pk)
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_candidate(patient, row['id'], actor=actor, action='CONFIRM',
                expected_revision=row['revision_number'], expected_source=row['current_source_token'], checked_original=True), pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(PermissionDenied if change == 'membership' else OrderingConflict):
            future.result(timeout=15)
    assert not CandidateRevision.objects.filter(candidate_id=row['id']).exists()


def test_new_uncollected_source_invalidates_a_waiting_selection(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-pg-selection')
    document, _ = _collect(patient)
    prior = resolve_ordering(patient)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            add_manual_fact(patient, document.pk, actor=patient.account, page_number=1,
                            category='DIAGNOSIS', text='临床诊断：胰腺癌。')
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: select_ordering(patient, actor=patient.account,
                mode='CANDIDATE', candidate_id=prior['candidates'][0]['id'], expected_revision=0,
                expected_fingerprint=prior['fingerprint']), pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(OrderingConflict):
            future.result(timeout=15)
    assert not DisplaySelection.objects.filter(patient=patient).exists()
    assert resolve_ordering(patient)['complete'] is False


def test_two_waiting_collectors_commit_one_complete_set_without_deduplicating_occurrences(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-pg-collection')
    parsed_facts(patient, ['出院诊断：肺癌；肺癌。'])
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
            assert len({blocker, first_pid, second_pid}) == 3
            # PostgreSQL queues the second tuple lock behind the first waiter,
            # which itself waits on the original transaction. Observe both real
            # edges instead of requiring both waiters to name the root blocker.
            wait_until_backend_is_blocked_by(_fresh_state, request_pid=second_pid, blocker_pid=first_pid)
            futures = [first, second]
        results = [future.result(timeout=20) for future in futures]
    assert results[0][0].pk == results[1][0].pk
    assert CollectionRun.objects.filter(patient=patient).count() == 1
    assert CancerCandidate.objects.filter(patient=patient).count() == 2
    assert resolve_ordering(patient)['profile'] == 'LUNG'


def test_actual_account_purge_on_another_backend_invalidates_held_review_form(django_user_model):
    from apps.accounts.deletion import request_account_deletion, purge_account_deletion
    from apps.accounts.models import Account

    _, patient = _patient(django_user_model, 'cancer-pg-author-purge')
    _collect(patient)
    actor = django_user_model.objects.create(phone_hash=hashlib.sha256(b'cancer-pg-reviewer').hexdigest(), phone_encrypted='synthetic')
    actor_id = actor.pk
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    row = _row(patient)
    revise_candidate(patient, row['id'], actor=actor, action='CONFIRM', expected_revision=0,
                      expected_source=row['current_source_token'], checked_original=True)
    held = _row(patient)
    def purge():
        pid = backend_pid()
        job = request_account_deletion(actor_id, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        purge_account_deletion(job.pk)
        assert not Account.objects.filter(pk=actor_id).exists()
        return pid
    with ThreadPoolExecutor(max_workers=1) as pool:
        other_pid = pool.submit(thread_call, purge).result(timeout=20)
    assert other_pid != backend_pid() and CandidateRevision.objects.get().author_id is None
    with pytest.raises(OrderingConflict):
        revise_candidate(patient, held['id'], actor=patient.account, action='CONFIRM', expected_revision=held['revision_number'],
                          expected_source=held['current_source_token'], checked_original=True)
    assert _row(patient)['status'] == 'PENDING' and resolve_ordering(patient)['profile'] == 'GENERAL'
