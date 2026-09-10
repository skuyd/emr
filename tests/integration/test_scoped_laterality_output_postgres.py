"""Actual independent PostgreSQL transactions, committed revisions and purge."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from django.db import connection, transaction

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.facts.models import FactRevision
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import FactConflict, revise_fact
from apps.lesions.services import rename_lesion
from apps.patients.access import authorize_patient
from apps.patients.models import PatientMembership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_detail_viewer import _patient
from tests.exports.test_lesion_portable_formats import prepared
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_scoped_laterality import confirm
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated scoped laterality output PostgreSQL database')


def blocked(pool, callback, pids):
    blocker = backend_pid()
    future = pool.submit(thread_call, callback, pids)
    waiter = pids.get(timeout=10)
    assert waiter != blocker
    wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
    return future


@pytest.mark.parametrize('operation', ['ordinary_confirm', 'replacement'])
def test_waiting_scope_write_rejects_committed_parent_revision(django_user_model, operation):
    from tests.facts.test_laterality_scope_operations import expected, new_member
    from apps.facts.laterality_services import replace_laterality_scope

    patient, _, parent, child = fixture(django_user_model, 'pg-scope-write-' + operation)
    confirm(patient, parent)
    parent_revision, parent_source = expected(parent)
    child_revision, child_source = expected(child)
    value, ranges = new_member(parent, child)
    def write():
        if operation == 'ordinary_confirm':
            return revise_fact(patient, child.pk, actor=patient.account, action='CONFIRM', checked_original=True,
                expected_revision=child_revision, expected_source=child_source,
                expected_parent_revision=parent_revision, expected_parent_source=parent_source)
        return replace_laterality_scope(patient, actor=patient.account, fact_id=child.pk, parent_id=parent.pk,
            expected_revision=child_revision, expected_source=child_source,
            expected_parent_revision=parent_revision, expected_parent_source=parent_source,
            scope_kind='NAMED_MEMBERS_ONLY', value=value, ranges=ranges, checked_original=True, confirm=True)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            confirm(patient, parent, action='REVOKE')
            future = blocked(pool, write, pids)
        with pytest.raises(FactConflict):
            future.result(timeout=20)
    assert not FactRevision.objects.filter(fact=child).exists()
    assert not child.document.facts.filter(origin='MANUAL').exists()


@pytest.mark.parametrize('change', ['parent', 'intermediate_author_purge'])
def test_waiting_output_check_sees_real_commit_and_author_cleanup(django_user_model, change):
    patient, reports, original, scope = prepared(django_user_model, 'pg-lesion-output-' + change)
    actor = None
    if change == 'intermediate_author_purge':
        _, own = _patient(django_user_model, 'pg-lesion-intermediate-author')
        actor = own.account
        PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
        lesion = patient.lesions.get()
        rename_lesion(patient, actor=actor, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name='历史作者名称')
        lesion.refresh_from_db()
        rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk, expected_revision=lesion.revision_number, name='当前作者名称')
        original = build_snapshot(patient, scope)
        deletion = request_account_deletion(actor.pk, document_dispatch=lambda *_: None, account_dispatch=lambda *_: None)
    pids = Queue()
    def check():
        with transaction.atomic():
            authorize_patient(patient, patient.account, 'export', lock=True)
            assert_snapshot_current(patient, original)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'parent':
                confirm(patient, reports[0].fields.get(field_key='lesion.site'), action='REVOKE')
            else:
                assert purge_account_deletion(deletion.pk).outcome == AccountDeletionOutcome.PURGED
                # The same parent serialization follows the committed cleanup; no account lock is acquired by check().
                authorize_patient(patient, patient.account, 'export', lock=True)
            future = blocked(pool, check, pids)
        with pytest.raises(SnapshotChanged):
            future.result(timeout=20)
    if actor:
        assert not actor.__class__.objects.filter(pk=actor.pk).exists()
        assert patient.lesions.get().revisions.filter(operation__author__isnull=True).exists()


def test_waiting_new_output_does_not_publish_an_invalidated_assignment(django_user_model):
    patient, reports, _, scope = prepared(django_user_model, 'pg-lesion-new-output')
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            confirm(patient, reports[0].fields.get(field_key='lesion.site'), action='REVOKE')
            future = blocked(pool, lambda: build_snapshot(patient, scope), pids)
        with pytest.raises(ExportInputError):
            future.result(timeout=20)
