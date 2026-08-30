import uuid

from django.core.exceptions import ImproperlyConfigured

from apps.processing.runner import ExecutionResult, ExecutionState
from apps.processing import tasks


def test_process_document_schedules_the_exact_persisted_retry(monkeypatch):
    run_id = uuid.uuid4()
    pipeline = object()
    scheduled = []
    monkeypatch.setattr(tasks, "get_processing_pipeline", lambda: pipeline)
    monkeypatch.setattr(
        tasks,
        "run_processing",
        lambda candidate, selected: ExecutionResult(
            state=ExecutionState.RETRY_SCHEDULED,
            run_id=uuid.UUID(str(candidate)),
            retry_delay=300,
        ),
    )
    monkeypatch.setattr(
        tasks,
        "safe_enqueue_processing",
        lambda candidate, *, countdown=0: scheduled.append((candidate, countdown)) or True,
    )

    payload = tasks.process_document.run(str(run_id))

    assert payload == {"state": "RETRY_SCHEDULED", "run_id": str(run_id), "retry_delay": 300}
    assert scheduled == [(run_id, 300)]


def test_process_document_leaves_queued_work_untouched_when_pipeline_is_not_configured(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "get_processing_pipeline",
        lambda: (_ for _ in ()).throw(ImproperlyConfigured("contains-private-config")),
    )

    payload = tasks.process_document.run(str(uuid.uuid4()))

    assert payload == {"state": "PIPELINE_NOT_CONFIGURED"}


def test_safe_enqueue_swallows_broker_failure_and_never_sends_private_metadata(monkeypatch):
    run_id = uuid.uuid4()
    calls = []

    def unavailable(*, args, countdown):
        calls.append((args, countdown))
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(tasks.process_document, "apply_async", unavailable)

    assert tasks.safe_enqueue_processing(run_id, countdown=60) is False
    assert calls == [([str(run_id)], 60)]


def test_recover_task_uses_safe_dispatcher(monkeypatch):
    run_id = uuid.uuid4()
    observed = {}

    def recover(*, dispatch):
        observed["dispatch"] = dispatch
        return (run_id,)

    monkeypatch.setattr(tasks, "recover_processing_runs", recover)

    payload = tasks.recover_stale_runs.run()

    assert observed["dispatch"] is tasks.safe_enqueue_processing
    assert payload == {"recovered": [str(run_id)], "count": 1}
