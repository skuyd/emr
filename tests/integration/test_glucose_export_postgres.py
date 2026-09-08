"""Selected glucose material must serialize with actual source and author changes."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from uuid import uuid4

from django.db import connection, transaction
import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.exports.errors import SnapshotChanged
from apps.glucose.exporting import assert_material_current, selected_material
from apps.glucose.services import create_record, revise_record
from apps.patients.access import authorize_patient
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.glucose.test_export_material import snapshot
from tests.glucose.test_payloads import payload
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated PostgreSQL glucose database')


def authored_record(patient, actor, *, revision):
    record = create_record(patient, patient.account if revision else actor, payload(), creation_key=uuid4()).record
    if revision:
        record = revise_record(patient, actor, record.pk, action='CORRECT', expected_revision=0,
                               changes=payload(value='8.6'))
    return record


@pytest.mark.parametrize('change', ['source', 'author_purge', 'revision_author_purge'])
def test_waiting_material_check_observes_committed_source_or_author_change(django_user_model, change):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-material-wait-' + change)
    if change == 'source':
        from tests.glucose.factories import lab_source
        from tests.glucose.test_sources import import_source
        _, _, _, _, observation = lab_source(django_user_model, patient=patient)
        record = import_source(patient, observation).record
    else:
        record = authored_record(patient, actor, revision=change == 'revision_author_purge')
    selection = {'glucose_record_ids': [str(record.pk)]}
    with transaction.atomic():
        authorize_patient(patient, patient.account, 'export', lock=True)
        first = snapshot(selection, selected_material(patient, selection, lock=True))
    if change != 'source':
        job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)

    def check():
        with transaction.atomic():
            authorize_patient(patient, patient.account, 'export', lock=True)
            assert_material_current(patient, first)

    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'source':
                from apps.labs.revisions import revise_observation
                revise_observation(patient.account, observation.pk, action='CORRECT', expected_revision=0,
                                   changes={'raw_value': '8.41'})
            else:
                assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
            blocker = backend_pid()
            future = pool.submit(thread_call, check, pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        # Normal COMMIT must finish its deferred FK checks without a lock cycle.
        with pytest.raises(SnapshotChanged):
            future.result(timeout=15)
    if change != 'source':
        record.refresh_from_db()
        assert record.current_data['raw_value'] == first['glucose_records'][0]['data']['raw_value']
        assert record.updated_by_id is None
        if change == 'revision_author_purge':
            assert record.created_by_id == patient.account_id
            assert record.revisions.get(sequence=1).author_id is None
        else:
            assert record.created_by_id is None


@pytest.mark.parametrize('revision', [False, True])
def test_author_purge_waits_for_current_material_then_invalidates_it(django_user_model, revision):
    _, patient, _, actor, _ = family(django_user_model, 'glucose-material-reader-first-' + str(revision))
    record = authored_record(patient, actor, revision=revision)
    selection = {'glucose_record_ids': [str(record.pk)]}
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            authorize_patient(patient, patient.account, 'export', lock=True)
            first = snapshot(selection, selected_material(patient, selection, lock=True))
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: purge_account_deletion(job.pk), pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
            assert_material_current(patient, first)
        assert future.result(timeout=15).outcome == AccountDeletionOutcome.PURGED
    with transaction.atomic():
        authorize_patient(patient, patient.account, 'export', lock=True)
        with pytest.raises(SnapshotChanged):
            assert_material_current(patient, first)


def test_existing_daily_snapshot_waits_for_author_purge_without_upgrading_patient_guard(django_user_model):
    from apps.exports.content import build_snapshot
    from apps.self_records.services import create_record as create_daily_record
    from tests.self_records.test_export_integration import selection
    from tests.self_records.test_payloads import payload as daily_payload

    _, patient, _, actor, _ = family(django_user_model, 'daily-material-purge-parent')
    record = create_daily_record(patient, actor, daily_payload(), creation_key=uuid4()).record
    chosen = selection(record)
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)

    def build():
        with transaction.atomic():
            authorize_patient(patient, patient.account, 'export', lock=True)
            return build_snapshot(patient, chosen)

    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
            blocker = backend_pid()
            future = pool.submit(thread_call, build, pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        material = future.result(timeout=15)
    assert material['self_records'][0]['id'] == str(record.pk)
    assert material['self_records'][0]['created_by'] is None
    assert material['self_records'][0]['updated_by'] is None


@pytest.mark.parametrize('target', ['export', 'share'])
def test_existing_selected_output_rejects_committed_author_purge_without_deadlock(django_user_model, target):
    from apps.exports.errors import ExportUnavailable
    from apps.exports.services import create_preview, get_preview
    from apps.patients.sharing import ShareUnavailable, create_share, exchange_share_token, authorize_share
    from apps.self_records.services import create_record as create_daily_record
    from tests.documents.test_detail_viewer import _patient
    from tests.self_records.test_export_integration import selection
    from tests.self_records.test_payloads import payload as daily_payload

    owner_client, patient, _, actor, _ = family(django_user_model, 'daily-purge-existing-' + target)
    record = create_daily_record(patient, actor, daily_payload(), creation_key=uuid4()).record
    if target == 'export':
        assert owner_client.get('/records/').status_code == 200
        key = owner_client.session.session_key
        output = create_preview(patient, key, selection(record), actor=patient.account)
        check = lambda: get_preview(patient, key, output.pk, actor=patient.account)
        expected_error = ExportUnavailable
    else:
        output = create_share(patient, patient.account, selection(record))
        reader, own = _patient(django_user_model, 'daily-purge-reader')
        assert reader.get('/shared/open/').status_code == 200
        key = reader.session.session_key
        exchange_share_token(output.token, own.account, key)
        output = output.share
        check = lambda: authorize_share(output.pk, own.account, key)
        expected_error = ShareUnavailable
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
            blocker = backend_pid()
            future = pool.submit(thread_call, check, pids)
            waiter = pids.get(timeout=10)
            assert waiter != blocker
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(expected_error):
            future.result(timeout=15)
    output.refresh_from_db()
    assert output.snapshot == {}
    if target == 'export':
        assert output.status == 'INVALIDATED'
    else:
        assert output.invalidated_at is not None
