"""Complete-report confirmation races on real PostgreSQL; synthetic inputs only."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
import uuid

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
import pytest

from apps.documents.models import Document
from apps.labs.batch_confirmation import confirmation_preview, confirm_reports
from apps.labs.models import LabConfirmationBatch, ObservationRevision
from apps.labs.reports import correct_report, decide_relation, report_relations
from apps.labs.revisions import RevisionConflict, effective_observation, revise_observation
from apps.operations.models import AuditEvent
from apps.patients.access import change_membership
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.documents.test_detail_viewer import _patient
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.labs.test_report_relations import report
from tests.labs.test_report_revision_versions import SOURCE, next_report_version
from tests.patients.test_family_access import family


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the disposable PostgreSQL integration database')


@pytest.mark.parametrize('same_operation', [True, False])
def test_concurrent_confirmation_writes_each_result_and_receipt_once(django_user_model, same_operation):
    _, patient = _patient(django_user_model, 'pg-batch-duplicate')
    _, first, _ = report(patient)
    _, duplicate, _ = report(patient)
    _, other, _ = report(patient, number='B200', value='7')
    tokens = [item['token'] for item in confirmation_preview(patient)]
    operation_id = uuid.uuid4()
    pids = Queue()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            saved = confirm_reports(patient, patient.account, tokens, operation_id=operation_id)
            blocker = backend_pid()
            concurrent = pool.submit(thread_call, lambda: confirm_reports(
                patient, patient.account, tokens,
                operation_id=operation_id if same_operation else uuid.uuid4(),
            ), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        if same_operation:
            assert concurrent.result(timeout=20) == saved
        else:
            with pytest.raises(RevisionConflict):
                concurrent.result(timeout=20)

    assert saved['report_count'] == 2
    assert saved['confirmed_count'] == 3
    assert saved['already_confirmed_count'] == saved['skipped_count'] == saved['remaining_count'] == 0
    assert LabConfirmationBatch.objects.count() == 1
    assert LabConfirmationBatch.objects.get().result == saved
    assert ObservationRevision.objects.count() == 3
    assert AuditEvent.objects.filter(action='lab_revised', reason_code='confirm').count() == 3
    for row, raw_value in [(first, '5'), (duplicate, '5'), (other, '7')]:
        row.refresh_from_db()
        revision, = row.revisions.all()
        assert row.revision_number == revision.sequence == 1
        assert revision.action == 'CONFIRM' and revision.author_id == patient.account_id
        assert revision.before['raw_value'] == revision.after['raw_value'] == raw_value
        assert effective_observation(row).review_state == 'CONFIRM'


@pytest.mark.parametrize('change', ['result', 'report', 'association'])
def test_committed_edit_invalidates_waiting_batch_without_partial_confirmation(django_user_model, change):
    _, patient = _patient(django_user_model, 'pg-batch-edit-' + change)
    _, changed, unit = report(patient)
    report(patient)
    _, untouched, _ = report(patient, number='B200')
    tokens = [item['token'] for item in confirmation_preview(patient)]
    association, = report_relations(patient)
    pids = Queue()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'result':
                revise_observation(patient.account, changed.pk, action='CORRECT',
                                   changes={'raw_value': '6'}, expected_revision=0)
            elif change == 'report':
                correct_report(patient, patient.account, unit.pk, {'institution': '核对后的合成医院'},
                               expected_revision=0, source_evidence=SOURCE,
                               rationale='合成原件字段核对', operation_id='concurrent-report-edit')
            else:
                decide_relation(patient, patient.account, association.pk, 'UNDO',
                                expected_revision=association.revision_number,
                                rationale='合成原件归属核对', operation_id='concurrent-association-edit')
            blocker = backend_pid()
            pending = pool.submit(thread_call, lambda: confirm_reports(
                patient, patient.account, tokens, operation_id=uuid.uuid4(),
            ), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(RevisionConflict):
            pending.result(timeout=20)

    assert not ObservationRevision.objects.filter(action='CONFIRM').exists()
    assert not untouched.revisions.exists()
    assert not LabConfirmationBatch.objects.exists()
    if change == 'result':
        changed.refresh_from_db()
        assert effective_observation(changed).raw_value == '6'


def test_parsing_publication_during_document_lock_wait_invalidates_batch(django_user_model):
    _, patient = _patient(django_user_model, 'pg-batch-publication')
    _, previous, unit = report(patient)
    _, untouched, _ = report(patient, number='B200')
    tokens = [item['token'] for item in confirmation_preview(patient)]
    pids = Queue()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            current, = next_report_version(unit)
            blocker = backend_pid()
            pending = pool.submit(thread_call, lambda: confirm_reports(
                patient, patient.account, tokens, operation_id=uuid.uuid4(),
            ), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(RevisionConflict):
            pending.result(timeout=20)

    previous.parsing_version.refresh_from_db()
    current.parsing_version.refresh_from_db()
    assert not previous.parsing_version.active and current.parsing_version.active
    assert not ObservationRevision.objects.exists()
    assert not untouched.revisions.exists()
    assert not LabConfirmationBatch.objects.exists()


@pytest.mark.parametrize('revoke', [True, False], ids=['revoked', 'downgraded'])
@pytest.mark.parametrize('replay', [False, True], ids=['first-submit', 'receipt-replay'])
def test_waiting_confirmation_rechecks_revoked_write_access(django_user_model, revoke, replay):
    _, patient, _, actor, membership = family(django_user_model, 'pg-batch-access')
    _, row, _ = report(patient)
    tokens = [item['token'] for item in confirmation_preview(patient)]
    operation_id = uuid.uuid4()
    if replay:
        confirm_reports(patient, actor, tokens, operation_id=operation_id)
    pids = Queue()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            change_membership(patient, patient.account, membership.pk, revoke=revoke,
                              role=None if revoke else 'VIEWER', expected_revision=0)
            blocker = backend_pid()
            pending = pool.submit(thread_call, lambda: confirm_reports(
                patient, actor, tokens, operation_id=operation_id,
            ), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(PermissionDenied):
            pending.result(timeout=20)

    row.refresh_from_db()
    assert row.revision_number == int(replay)
    assert ObservationRevision.objects.count() == int(replay)
    assert LabConfirmationBatch.objects.count() == int(replay)
    membership.refresh_from_db()
    assert membership.revoked_at is not None if revoke else membership.role == 'VIEWER'


def test_account_deactivation_during_document_lock_wait_prevents_confirmation(django_user_model):
    _, patient, _, actor, _ = family(django_user_model, 'pg-batch-account')
    document, row, _ = report(patient)
    tokens = [item['token'] for item in confirmation_preview(patient)]
    pids = Queue()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            Document.objects.select_for_update().get(pk=document.pk)
            blocker = backend_pid()
            pending = pool.submit(thread_call, lambda: confirm_reports(
                patient, actor, tokens, operation_id=uuid.uuid4(),
            ), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
            django_user_model.objects.filter(pk=actor.pk).update(is_active=False)
        with pytest.raises(PermissionDenied):
            pending.result(timeout=20)

    row.refresh_from_db()
    assert row.revision_number == 0
    assert not ObservationRevision.objects.exists()
    assert not LabConfirmationBatch.objects.exists()


def test_relation_commit_during_preview_cannot_confirm_items_presented_as_skipped(django_user_model, monkeypatch):
    from apps.labs import batch_confirmation

    client, patient = _patient(django_user_model, 'pg-batch-preview-relation')
    report(patient, value='5')
    report(patient, value='6')
    association, = report_relations(patient)
    assert association.state == 'REVIEW'
    entered, release, pids = Event(), Event(), Queue()
    assign_groups = batch_confirmation.assign_report_groups

    def pause_before_assigning_groups(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=20)
        return assign_groups(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(batch_confirmation, 'assign_report_groups', pause_before_assigning_groups)
        with ThreadPoolExecutor(max_workers=1) as pool:
            preview = pool.submit(thread_call, lambda: confirmation_preview(patient), pids)
            try:
                assert entered.wait(timeout=20)
                assert pids.get(timeout=10) != backend_pid()
                decide_relation(patient, patient.account, association.pk, 'DIFFERENT',
                                expected_revision=association.revision_number,
                                rationale='合成原件证实两份独立报告', operation_id='preview-relation-change')
                assert not connection.in_atomic_block
            finally:
                release.set()
            displayed = preview.result(timeout=20)

    association.refresh_from_db()
    assert association.state == 'DIFFERENT'
    assert len(displayed) == 2
    assert sum(item['pending_count'] for item in displayed) == 0
    assert sum(item['skipped_count'] for item in displayed) == 2
    response = client.post('/labs/reports/batch-confirmation/', {
        'patient_id': str(patient.pk), 'operation_id': str(uuid.uuid4()),
        'report': [item['token'] for item in displayed],
    })
    assert response.status_code == 409
    assert not ObservationRevision.objects.exists()
    assert not LabConfirmationBatch.objects.exists()
