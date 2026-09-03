import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from apps.documents.models import ProcessingRun, ProcessingStage
from apps.processing import tasks
from apps.processing.runner import run_processing


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
    help = "Process the durable OCR queue without an external broker in local development."

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
            close_old_connections()
            run_ids = _due_run_ids(limit)
            if run_ids and pipeline is None:
                pipeline = tasks.get_processing_pipeline()
            for run_id in run_ids:
                result = run_processing(run_id, pipeline)
                self.stdout.write(f"processing_result={result.state.value} run_id={result.run_id}")
            if options["once"]:
                return
            if not run_ids:
                time.sleep(poll_interval)
