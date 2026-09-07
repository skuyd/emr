from datetime import timedelta
import re
import sqlite3
from uuid import uuid4

from django.core.management import call_command, CommandError
from django.db import OperationalError
from django.test import override_settings
from django.utils import timezone
import pytest

from apps.documents.models import ProcessingStage
from apps.processing import tasks
from apps.processing.management.commands import run_local_processing_worker as local_worker
from apps.processing.runner import PipelineResult
from tests.processing.test_runner import make_run


pytestmark = pytest.mark.django_db(transaction=True)


def _queue_reset_sms(django_user_model):
    from apps.accounts.authentication import begin_password_reset
    from apps.accounts.crypto import hash_phone
    from apps.accounts.models import SmsDeliveryJob

    django_user_model.objects.create_user(
        phone_hash=hash_phone("+8613800138000"), phone_encrypted="synthetic",
        password="Original synthetic passphrase",
    )
    begin_password_reset("13800138000", "127.0.0.1")
    return SmsDeliveryJob.objects.get()


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_delivers_due_sms_without_a_broker_or_ocr_pipeline(django_user_model, monkeypatch):
    from apps.accounts.models import OtpChallenge, SmsDeliveryJob
    from tests.accounts.fakes import RecordingSmsProvider

    job = _queue_reset_sms(django_user_model)
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.sms_delivery.get_sms_provider", lambda: provider)

    def unexpected_pipeline():
        pytest.fail("SMS-only work must not initialize OCR")

    monkeypatch.setattr(tasks, "get_processing_pipeline", unexpected_pipeline)
    call_command("run_local_processing_worker", once=True, verbosity=0)

    assert len(provider.codes) == 1
    assert not SmsDeliveryJob.objects.exists()
    assert OtpChallenge.objects.get(pk=job.challenge_id).delivery_status == OtpChallenge.DeliveryStatus.SENT


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_prioritizes_sms_before_loading_ocr(django_user_model, monkeypatch):
    from tests.accounts.fakes import RecordingSmsProvider

    _queue_reset_sms(django_user_model)
    _batch, _document, queued = make_run(django_user_model)
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.sms_delivery.get_sms_provider", lambda: provider)

    class Pipeline:
        def run(self, context):
            return PipelineResult.original_only()

    def factory():
        assert len(provider.codes) == 1
        return Pipeline()

    monkeypatch.setattr(tasks, "get_processing_pipeline", factory)
    call_command("run_local_processing_worker", once=True, verbosity=0)

    queued.refresh_from_db()
    assert queued.stage == ProcessingStage.NO_STRUCTURED_RESULT


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_preserves_sms_backoff_after_provider_failure(django_user_model, monkeypatch):
    from tests.accounts.fakes import FailingSmsProvider

    job = _queue_reset_sms(django_user_model)
    monkeypatch.setattr("apps.accounts.sms_delivery.get_sms_provider", lambda: FailingSmsProvider())
    call_command("run_local_processing_worker", once=True, verbosity=0)

    job.refresh_from_db()
    assert job.attempt_count == 1
    assert job.next_attempt_at > timezone.now()
    assert job.lease_until is None
    call_command("run_local_processing_worker", once=True, verbosity=0)
    job.refresh_from_db()
    assert job.attempt_count == 1


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_services_sms_arriving_between_documents(django_user_model, monkeypatch):
    from tests.accounts.fakes import RecordingSmsProvider

    _batch, _document, _first = make_run(django_user_model)
    _batch, _document, _second = make_run(django_user_model)
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.sms_delivery.get_sms_provider", lambda: provider)
    processed = []

    class Pipeline:
        def run(self, context):
            if not processed:
                _queue_reset_sms(django_user_model)
            else:
                assert len(provider.codes) == 1
            processed.append(context.run_id)
            return PipelineResult.original_only()

    monkeypatch.setattr(tasks, "get_processing_pipeline", Pipeline)
    call_command("run_local_processing_worker", once=True, verbosity=0)

    assert len(processed) == 2
    assert len(provider.codes) == 1


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


def _sqlite_error(code):
    cause = sqlite3.OperationalError("synthetic database contention")
    cause.sqlite_errorcode = code
    error = OperationalError("synthetic database contention")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, 517])
@pytest.mark.parametrize("failure_point", ["_due_run_ids", "run_processing"])
@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_survives_sqlite_contention_and_processes_the_queued_document(
    django_user_model, monkeypatch, error_code, failure_point,
):
    _batch, _document, queued = make_run(django_user_model)

    class Pipeline:
        def run(self, context):
            return PipelineResult.original_only()

    class StopPolling(Exception):
        pass

    operation = getattr(local_worker, failure_point)
    attempts = []

    def temporarily_busy(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise _sqlite_error(error_code)
        return operation(*args, **kwargs)

    def stop_after_processing(_interval):
        queued.refresh_from_db()
        if queued.stage == ProcessingStage.NO_STRUCTURED_RESULT:
            raise StopPolling()

    monkeypatch.setattr(tasks, "get_processing_pipeline", Pipeline)
    monkeypatch.setattr(local_worker, failure_point, temporarily_busy)
    monkeypatch.setattr(local_worker.time, "sleep", stop_after_processing)

    with pytest.raises(StopPolling):
        call_command("run_local_processing_worker", poll_interval=0.1, verbosity=0)

    queued.refresh_from_db()
    assert queued.stage == ProcessingStage.NO_STRUCTURED_RESULT
    assert len(attempts) >= 2


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_does_not_hide_non_lock_database_errors(monkeypatch):
    def broken_query(_limit):
        raise _sqlite_error(sqlite3.SQLITE_ERROR)

    monkeypatch.setattr(local_worker, "_due_run_ids", broken_query)
    with pytest.raises(OperationalError):
        call_command("run_local_processing_worker", verbosity=0)


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_once_reports_sqlite_contention_instead_of_polling_forever(monkeypatch):
    def busy_query(_limit):
        raise _sqlite_error(sqlite3.SQLITE_BUSY)

    monkeypatch.setattr(local_worker, "_due_run_ids", busy_query)
    with pytest.raises(OperationalError):
        call_command("run_local_processing_worker", once=True, verbosity=0)


@override_settings(DEBUG=True, PRODUCTION_DEPLOYMENT=False)
def test_local_worker_recovers_work_abandoned_by_a_dead_worker(django_user_model, monkeypatch):
    _batch, _document, abandoned = make_run(django_user_model)
    abandoned.stage = ProcessingStage.OCR
    abandoned.lease_token = uuid4()
    abandoned.heartbeat_at = timezone.now() - timedelta(hours=1)
    abandoned.save(update_fields=["stage", "lease_token", "heartbeat_at", "updated_at"])

    class Pipeline:
        def run(self, context):
            return PipelineResult.original_only()

    monkeypatch.setattr(tasks, "get_processing_pipeline", Pipeline)
    call_command("run_local_processing_worker", once=True, verbosity=0)

    abandoned.refresh_from_db()
    assert abandoned.stage == ProcessingStage.NO_STRUCTURED_RESULT
