from datetime import timedelta
import re

from django.core.management import call_command, CommandError
from django.test import override_settings
from django.utils import timezone
import pytest

from apps.documents.models import ProcessingStage
from apps.processing import tasks
from apps.processing.runner import PipelineResult
from tests.processing.test_runner import make_run


pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_once_processes_due_runs_and_reuses_pipeline(
    django_user_model, monkeypatch
):
    _batch, _document, queued = make_run(django_user_model)
    _batch, _document, retry_due = make_run(django_user_model)
    _batch, _document, retry_later = make_run(django_user_model)
    now = timezone.now()
    retry_due.next_retry_at = now - timedelta(seconds=1)
    retry_due.save(update_fields=["next_retry_at", "updated_at"])
    retry_later.next_retry_at = now + timedelta(minutes=5)
    retry_later.save(update_fields=["next_retry_at", "updated_at"])

    processed = []
    factory_calls = []

    class Pipeline:
        def run(self, context):
            processed.append(context.run_id)
            return PipelineResult.original_only()

    def pipeline_factory():
        factory_calls.append(True)
        return Pipeline()

    monkeypatch.setattr(tasks, "get_processing_pipeline", pipeline_factory)

    try:
        call_command("run_local_processing_worker", once=True, verbosity=0)
    except CommandError as error:
        pytest.fail(f"local processing command is unavailable: {error}")

    queued.refresh_from_db()
    retry_due.refresh_from_db()
    retry_later.refresh_from_db()
    assert processed == [queued.pk, retry_due.pk]
    assert queued.stage == ProcessingStage.NO_STRUCTURED_RESULT
    assert retry_due.stage == ProcessingStage.NO_STRUCTURED_RESULT
    assert retry_later.stage == ProcessingStage.QUEUED
    assert factory_calls == [True]


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_skips_runs_for_documents_pending_deletion(
    django_user_model, monkeypatch
):
    _batch, document, queued = make_run(django_user_model)
    document.deleted_at = timezone.now()
    document.save(update_fields=["deleted_at", "updated_at"])

    def unexpected_pipeline_factory():
        raise AssertionError("deleted documents must not initialize the processing pipeline")

    monkeypatch.setattr(tasks, "get_processing_pipeline", unexpected_pipeline_factory)

    call_command("run_local_processing_worker", once=True, verbosity=0)

    queued.refresh_from_db()
    assert queued.stage == ProcessingStage.QUEUED


@override_settings(DEBUG=False, PRODUCTION_DEPLOYMENT=True)
def test_local_worker_refuses_to_run_in_production():
    with pytest.raises(CommandError, match="local development"):
        call_command("run_local_processing_worker", once=True, verbosity=0)


@pytest.mark.parametrize("poll_interval", [0.09, 60.01])
@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_rejects_poll_interval_outside_safe_range(poll_interval):
    with pytest.raises(
        CommandError,
        match=re.escape("--poll-interval must be between 0.1 and 60 seconds"),
    ):
        call_command(
            "run_local_processing_worker",
            once=True,
            poll_interval=poll_interval,
            verbosity=0,
        )


@pytest.mark.parametrize("limit", [0, 1001])
@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_rejects_limit_outside_safe_range(limit):
    with pytest.raises(
        CommandError,
        match=re.escape("--limit must be between 1 and 1000"),
    ):
        call_command(
            "run_local_processing_worker",
            once=True,
            limit=limit,
            verbosity=0,
        )
