"""Single-report review races on the disposable PostgreSQL test database."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import uuid

from django.db import connection, transaction
import pytest

from apps.labs.models import LabReportReviewEvent
from apps.labs.report_workspace import report_workspace, submit_report_workspace
from apps.labs.revisions import RevisionConflict, revise_observation
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_detail_viewer import _patient
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.labs.test_report_relations import report


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the disposable PostgreSQL integration database')


@pytest.mark.parametrize('same_operation', [True, False])
def test_two_windows_cannot_double_confirm_or_append(django_user_model, same_operation):
    _, patient = _patient(django_user_model, 'pg-report-workspace-' + str(same_operation))
    _, row, _ = report(patient)
    current = report_workspace(patient)['current']
    operation = uuid.uuid4()
    pids = Queue()

    def submit(operation_id):
        return submit_report_workspace(patient, patient.account, current['key'], current['token'],
            operation_id, {'observations': [{'id': str(row.pk), 'expected_revision': 0,
                                            'changes': {'raw_value': '7'}}]}, confirm=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            saved = submit(operation)
            blocker = backend_pid()
            waiting = pool.submit(thread_call, lambda: submit(operation if same_operation else uuid.uuid4()), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        if same_operation:
            assert waiting.result(timeout=20) == saved
        else:
            with pytest.raises(RevisionConflict):
                waiting.result(timeout=20)
    assert LabReportReviewEvent.objects.count() == 1
    assert list(row.revisions.values_list('action', flat=True)) == ['CORRECT', 'CONFIRM']


def test_committed_edit_rejects_waiting_review_before_any_write(django_user_model):
    _, patient = _patient(django_user_model, 'pg-report-workspace-stale')
    _, row, _ = report(patient)
    current = report_workspace(patient)['current']
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            revise_observation(patient.account, row.pk, action='CORRECT',
                               changes={'raw_value': '8'}, expected_revision=0)
            blocker = backend_pid()
            waiting = pool.submit(thread_call, lambda: submit_report_workspace(
                patient, patient.account, current['key'], current['token'], uuid.uuid4(),
                {'observations': [{'id': str(row.pk), 'expected_revision': 0,
                                   'changes': {'raw_value': '7'}}]}, confirm=True), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(RevisionConflict):
            waiting.result(timeout=20)
    assert not LabReportReviewEvent.objects.exists()
    assert list(row.revisions.values_list('action', flat=True)) == ['CORRECT']
