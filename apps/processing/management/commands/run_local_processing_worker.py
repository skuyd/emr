import sqlite3
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections, connection
from django.db.models import Q
from django.utils import timezone

from apps.accounts.sms_delivery import deliver_sms_job, due_sms_deliveries
from apps.documents.models import ProcessingRun, ProcessingStage
from apps.processing import tasks
from apps.processing.runner import recover_processing_runs, run_processing


def _is_sqlite_lock_error(error):
    code = getattr(error.__cause__, "sqlite_errorcode", None)
    return (
        connection.vendor == "sqlite"
        and isinstance(code, int)
        and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


def _due_run_ids(limit):
    now = timezone.now()
    return tuple(
        ProcessingRun.objects.filter(
            stage=ProcessingStage.QUEUED,
            document__deleted_at__isnull=True,
        )
        .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )


class Command(BaseCommand):
    help = "Process durable SMS, OCR and cloud-source scans without an external broker in local development."

    def _deliver_due_sms(self, limit):
        job_ids = due_sms_deliveries(limit=limit)
        for job_id in job_ids:
            result = deliver_sms_job(job_id)
            self.stdout.write(f"sms_delivery_result={result}")
        return len(job_ids)

    def _scan_due_cloud_sources(self, limit):
        from apps.cloud_imaging.scan_services import recover_scans, run_scan
        from apps.documents.backends import get_object_store

        pending = []
        recover_scans(dispatch=pending.append, limit=limit)
        store = get_object_store() if pending else None
        for scan_id in pending:
            run_scan(scan_id, store)
            self.stdout.write(f"cloud_scan_processed scan_id={scan_id}")
            self._deliver_due_sms(limit)
        return len(pending)

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Drain one snapshot of due work and exit.")
        parser.add_argument("--poll-interval", type=float, default=1.0)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not settings.DEBUG or settings.PRODUCTION_DEPLOYMENT:
            raise CommandError("This worker is restricted to local development.")
        poll_interval = options["poll_interval"]
        limit = options["limit"]
        if not 0.1 <= poll_interval <= 60:
            raise CommandError("--poll-interval must be between 0.1 and 60 seconds")
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000")

        pipeline = None
        while True:
            try:
                close_old_connections()
                sms_count = self._deliver_due_sms(limit)
                recover_processing_runs(limit=limit)
                run_ids = _due_run_ids(limit)
                if run_ids and pipeline is None:
                    pipeline = tasks.get_processing_pipeline()
                for run_id in run_ids:
                    result = run_processing(run_id, pipeline)
                    self.stdout.write(f"processing_result={result.state.value} run_id={result.run_id}")
                    sms_count += self._deliver_due_sms(limit)
                cloud_count = self._scan_due_cloud_sources(limit)
            except OperationalError as exc:
                if options["once"] or not _is_sqlite_lock_error(exc):
                    raise
                self.stderr.write("Local database is busy; retrying pending work.")
                close_old_connections()
                time.sleep(poll_interval)
                continue
            if options["once"]:
                return
            if not run_ids and not sms_count and not cloud_count:
                time.sleep(poll_interval)
