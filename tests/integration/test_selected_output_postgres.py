"""Selected retained output serializes with committed author changes."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from uuid import uuid4

from django.db import connection, transaction
import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.patients.access import authorize_patient
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated PostgreSQL selected output database')


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


def authored_output_selection(patient, actor):
    from apps.self_records.services import create_record
    from tests.self_records.test_payloads import payload

    record = create_record(patient, actor, payload(), creation_key=uuid4()).record
    return {'mode': 'documents', 'document_ids': [], 'self_record_ids': [str(record.pk)],
            'sections': ['patient', 'self_records'], 'details': True}


def existing_output(owner_client, patient, chosen, target, django_user_model):
    from apps.exports.errors import ExportUnavailable
    from apps.exports.services import create_preview, get_preview
    from apps.patients.sharing import ShareUnavailable, create_share, exchange_share_token, authorize_share
    from tests.documents.test_detail_viewer import _patient

    if target == 'export':
        assert owner_client.get('/records/').status_code == 200
        key = owner_client.session.session_key
        output = create_preview(patient, key, chosen, actor=patient.account)
        check = lambda: get_preview(patient, key, output.pk, actor=patient.account)
        expected_error = ExportUnavailable
    else:
        output = create_share(patient, patient.account, chosen)
        reader, own = _patient(django_user_model, 'daily-purge-reader')
        assert reader.get('/shared/open/').status_code == 200
        key = reader.session.session_key
        exchange_share_token(output.token, own.account, key)
        output = output.share
        check = lambda: authorize_share(output.pk, own.account, key)
        expected_error = ShareUnavailable
    return output, check, expected_error


@pytest.mark.parametrize('target', ['export', 'share'])
def test_existing_selected_output_rejects_committed_author_purge_without_deadlock(django_user_model, target):
    owner_client, patient, _, actor, _ = family(django_user_model, 'daily-purge-existing-' + target)
    chosen = authored_output_selection(patient, actor)
    output, check, expected_error = existing_output(owner_client, patient, chosen, target, django_user_model)
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
