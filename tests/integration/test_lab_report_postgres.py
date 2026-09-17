"""Real PostgreSQL locks for admission and report decisions; synthetic inputs only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from django.db import close_old_connections, connection
import pytest

from apps.documents.intake import run_intake
from apps.documents.models import Document, ProcessingRun, UploadBatch, UploadIntake
from apps.labs.models import ReportAssociation, ReportAssociationEvent
from apps.labs.reports import ReportDecisionConflict, decide_relation, report_relations
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.documents.test_lab_intake import lab_page, stage
from tests.labs.test_report_relations import report
from tests.processing.test_pipeline import _pipeline


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the disposable PostgreSQL integration database')


def in_connection(action):
    close_old_connections()
    try:
        return action()
    finally:
        close_old_connections()


def test_duplicate_intake_delivery_recognizes_and_admits_once(django_user_model):
    _, patient = _patient(django_user_model, 'postgres-intake-delivery')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    pipeline = _pipeline(store, lab_page())
    recognize = pipeline.recognize_upload
    entered, release = Event(), Event()
    calls = []

    def paused_recognition(*args):
        calls.append(True)
        entered.set()
        assert release.wait(timeout=20)
        return recognize(*args)

    pipeline.recognize_upload = paused_recognition
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(in_connection, lambda: run_intake(item.pk, pipeline, store))
        try:
            assert entered.wait(timeout=20)
            duplicate = pool.submit(in_connection, lambda: run_intake(item.pk, pipeline, store))
            assert duplicate.result(timeout=20) == 'BUSY'
        finally:
            release.set()
        assert first.result(timeout=20) == 'SETTLED'
    assert calls == [True]
    assert Document.objects.count() == ProcessingRun.objects.count() == 1
    assert UploadIntake.objects.get(item=item).state == 'SETTLED'


def test_simultaneous_batch_main_and_continuation_keep_both_sources(django_user_model):
    _, patient = _patient(django_user_model, 'postgres-intake-batch')
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, created_by=patient.account, file_count=2)
    main, _ = stage(patient, store, batch=batch)
    continuation, _ = stage(patient, store, batch=batch, ordinal=2, shade=20)
    barrier = Barrier(2)

    def process(item, page):
        pipeline = _pipeline(store, page)
        recognize = pipeline.recognize_upload
        def simultaneous_recognition(*args):
            result = recognize(*args)
            barrier.wait(timeout=20)
            return result
        pipeline.recognize_upload = simultaneous_recognition
        return run_intake(item.pk, pipeline, store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(in_connection, lambda: process(main, lab_page(page_marker='第1页 共2页')))
        second = pool.submit(in_connection, lambda: process(continuation, lab_page(time='', page_marker='第2页 共2页')))
        assert first.result(timeout=30) in {'SETTLED', 'WAITING_BATCH'}
        assert second.result(timeout=30) in {'SETTLED', 'WAITING_BATCH'}
    main.refresh_from_db()
    continuation.refresh_from_db()
    assert main.validity['status'] == continuation.validity['status'] == 'ACCEPTED'
    assert main.document_id != continuation.document_id
    assert Document.objects.count() == ProcessingRun.objects.count() == 2
    assert UploadIntake.objects.filter(state='SETTLED').count() == 2


def test_simultaneous_relation_refresh_creates_one_relation_and_event(django_user_model):
    _, patient = _patient(django_user_model, 'postgres-report-refresh')
    report(patient)
    report(patient)
    barrier = Barrier(2)

    def refresh():
        barrier.wait(timeout=20)
        return report_relations(patient)[0].pk

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(in_connection, refresh) for _ in range(2)]
        assert results[0].result(timeout=30) == results[1].result(timeout=30)
    assert ReportAssociation.objects.count() == ReportAssociationEvent.objects.count() == 1
    assert ReportAssociation.objects.get().state == 'AUTO'


@pytest.mark.parametrize('retry', [False, True])
def test_concurrent_report_decisions_are_versioned_and_idempotent(django_user_model, retry):
    _, patient = _patient(django_user_model, 'postgres-report-decide')
    report(patient)
    report(patient)
    relation = report_relations(patient)[0]
    barrier = Barrier(2)

    def decide(operation, action):
        barrier.wait(timeout=20)
        try:
            return decide_relation(patient, patient.account, relation.pk, action,
                expected_revision=relation.revision_number, rationale='对照合成原件', operation_id=operation).state
        except ReportDecisionConflict:
            return 'STALE'

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(in_connection, lambda: decide('decision-a', 'UNDO'))
        second = pool.submit(in_connection, lambda: decide('decision-a' if retry else 'decision-b', 'UNDO' if retry else 'DIFFERENT'))
        states = [first.result(timeout=30), second.result(timeout=30)]
    relation.refresh_from_db()
    assert relation.revision_number == 2
    assert ReportAssociationEvent.objects.count() == 2
    if retry:
        assert states == ['UNDONE', 'UNDONE']
    else:
        assert states.count('STALE') == 1
        assert relation.state in {'UNDONE', 'DIFFERENT'}


def test_main_deletion_waits_for_reprocessing_evidence_guard_then_excludes_continuation(django_user_model, monkeypatch):
    from queue import Queue
    from apps.documents.deletion import request_document_deletion
    from apps.labs import reports
    from apps.labs.readmodels import effective_rows
    from apps.processing.runner import run_processing, ExecutionState
    from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
    from tests.documents.test_lab_intake import admitted_continuation_pair
    from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call

    _, patient = _patient(django_user_model, 'postgres-reparse-donor-delete')
    store = InMemoryObjectStore()
    main, continuation, pipelines = admitted_continuation_pair(patient, store)
    report_relations(patient)
    next_run = ProcessingRun.objects.create(document_id=continuation.document_id, parser_version='reparse-v2',
        task_type='reparse', idempotency_key=f'{continuation.document_id}:reparse-v2', attempt_number=2)
    resolver = reports.resolve_reprocessed_continuations
    release, pids, blocker_pids = Event(), Queue(), Queue()

    def paused_resolution(*args):
        resolved = resolver(*args)
        assert resolved[0].status == 'ACCEPTED'
        blocker_pids.put(backend_pid())
        assert release.wait(timeout=20)
        return resolved

    monkeypatch.setattr(reports, 'resolve_reprocessed_continuations', paused_resolution)
    with ThreadPoolExecutor(max_workers=2) as pool:
        processing = pool.submit(in_connection, lambda: run_processing(next_run.pk, pipelines[1]))
        try:
            blocker = blocker_pids.get(timeout=20)
            deletion = pool.submit(thread_call,
                lambda: request_document_deletion(patient, main.document_id, dispatch=lambda job: None), pids)
            waiter = pids.get(timeout=20)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        finally:
            release.set()
        assert processing.result(timeout=30).state == ExecutionState.SUCCEEDED
        deletion.result(timeout=30)
    assert Document.objects.get(pk=main.document_id).deleted_at is not None
    assert effective_rows(patient) == ()
    assert store.objects[continuation.document.original_object_key]


@pytest.mark.parametrize('retry', [False, True])
def test_concurrent_inherited_report_resolution_preserves_one_audited_decision(django_user_model, retry):
    from dataclasses import replace
    from apps.labs.reports import _from_snapshot, correct_report, report_source_token
    from tests.labs.test_report_revision_versions import SOURCE, next_report_version

    _, patient = _patient(django_user_model, 'postgres-report-resolution-' + str(retry))
    _, _, original = report(patient)
    correct_report(patient, patient.account, original.pk, {'institution': '核对医院'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='原件医院', operation_id='original')
    current, = next_report_version(original, (replace(_from_snapshot(original.automatic), report_number='NEW'),))
    token = report_source_token(current)
    barrier = Barrier(2)

    def resolve(operation, action):
        barrier.wait(timeout=20)
        try:
            correct_report(patient, patient.account, current.pk, {}, action=action, expected_revision=0,
                expected_source=token, source_evidence=SOURCE, rationale='核对新旧原件依据', operation_id=operation)
            return 'SAVED'
        except ReportDecisionConflict:
            return 'STALE'

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(in_connection, lambda: resolve('resolution-a', 'KEEP_REVISION'))
        second = pool.submit(in_connection, lambda: resolve('resolution-a' if retry else 'resolution-b',
                                                           'KEEP_REVISION' if retry else 'USE_AUTOMATIC'))
        states = [first.result(timeout=30), second.result(timeout=30)]
    current.refresh_from_db()
    assert current.revision_number == 1
    assert current.revisions.count() == original.revisions.count() == 1
    assert current.revisions.get().inherited_from_id == original.revisions.get().pk
    assert states.count('SAVED') == (2 if retry else 1)
    assert states.count('STALE') == (0 if retry else 1)
