from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Event

from django.db import connection
from django.utils import timezone
import pytest

from apps.cancer_ordering.models import CancerCandidate, CollectionRun
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.facts.models import Fact
from apps.facts.revisions import add_manual_fact
from apps.processing.models import ParsingVersion
from apps.processing.runner import ExecutionState, recover_processing_runs, run_processing
from tests.cancer_ordering.test_pipeline import _worker_case, _page
from tests.integration.test_family_postgres_concurrency import backend_pid, thread_call
from tests.processing.test_pipeline import _pipeline, _png_bytes, _Store


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires the isolated cancer-ordering PostgreSQL test database')


def test_committed_lease_recovery_fences_old_collector_and_new_worker_can_publish(django_user_model):
    document, run = _worker_case(django_user_model)
    pipeline = _pipeline(_Store(_png_bytes()), _page('出院诊断：肺癌。'))
    original_persist = pipeline._persist
    entered, release = Event(), Event()
    pids = Queue()
    def paused_persist(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=20)
        return original_persist(*args, **kwargs)
    pipeline._persist = paused_persist
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: run_processing(run.pk, pipeline), pids)
        other_pid = pids.get(timeout=10)
        try:
            assert entered.wait(timeout=15) and other_pid != backend_pid()
            # Use the real recovery service and its transaction, rather than
            # rewriting a lease in a model fixture or wrapping COMMIT in a mock.
            assert run.pk in recover_processing_runs(now=timezone.now() + timedelta(minutes=16))
        finally:
            release.set()
        assert future.result(timeout=20).state == ExecutionState.LEASE_LOST
    assert not ParsingVersion.objects.filter(processing_run=run).exists()
    assert not Fact.objects.filter(document=document).exists()
    assert not CollectionRun.objects.filter(document=document).exists()
    pipeline._persist = original_persist
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    assert CancerCandidate.objects.filter(document=document).count() == 1
    assert resolve_ordering(document.patient)['profile'] == 'LUNG'


def test_new_uncollected_source_commits_between_ready_collection_and_actual_publication(django_user_model):
    document, run = _worker_case(django_user_model)
    pipeline = _pipeline(_Store(_png_bytes()), _page('出院诊断：肺癌。'))
    entered, release = Event(), Event()
    pids = Queue()
    def pause_before_publication(context):
        result = pipeline.run(context)
        entered.set()
        assert release.wait(timeout=20)
        return result
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(thread_call, lambda: run_processing(run.pk, pause_before_publication), pids)
        other_pid = pids.get(timeout=10)
        try:
            assert entered.wait(timeout=15) and other_pid != backend_pid()
            ready = ParsingVersion.objects.get(processing_run=run)
            assert ready.status == 'READY' and not ready.active
            assert CollectionRun.objects.get(parsing_version=ready).status == 'COMPLETE'
            add_manual_fact(document.patient, document.pk, actor=document.patient.account,
                            page_number=1, category='DIAGNOSIS', text='临床诊断：胰腺癌。')
        finally:
            release.set()
        assert future.result(timeout=20).state == ExecutionState.SUCCEEDED
    state = resolve_ordering(document.patient)
    assert not state['complete'] and state['profile'] == 'GENERAL'
    assert CancerCandidate.objects.filter(document=document).count() == 1
    collect_current(document.patient, actor=document.patient.account)
    state = resolve_ordering(document.patient)
    assert state['complete'] and state['reason'] == 'reported_diagnoses_differ'
    assert state['profile'] == 'GENERAL'
