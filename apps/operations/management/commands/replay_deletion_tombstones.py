from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion
from apps.accounts.models import AccountDeletionJob
from apps.accounts.tasks import safe_enqueue_account_deletion
from apps.documents.backends import get_object_store
from apps.documents.deletion import DeletionOutcome, purge_document_deletion
from apps.documents.models import DocumentDeletionJob
from apps.documents.tasks import safe_enqueue_document_deletion
from apps.operations.tombstones import (
    InvalidTombstoneLog,
    decode_tombstone_log,
    replay_restore_tombstones,
)


class Command(BaseCommand):
    help = "Replay an external signed deletion log before restored traffic is opened."

    def add_arguments(self, parser):
        parser.add_argument("input", help="Explicit JSONL tombstone log path")
        parser.add_argument("--synchronous", action="store_true")
        parser.add_argument("--confirm", default="")

    def _synchronous_dispatchers(self, options):
        database_name = str(settings.DATABASES.get("default", {}).get("NAME", ""))
        storage_prefix = getattr(settings, "DOCUMENT_S3_PREFIX", "")
        if (
            not getattr(settings, "RESTORE_DRILL_MODE", False)
            or not database_name.endswith("_restore_drill")
            or not storage_prefix.startswith("restore-drill/")
            or options["confirm"] != "ISOLATED-RESTORE-DRILL"
        ):
            raise CommandError(
                "Synchronous replay requires isolated *_restore_drill database/storage targets and exact confirmation"
            )
        document_jobs = []
        account_jobs = []
        return document_jobs, account_jobs, document_jobs.append, account_jobs.append

    def _purge_synchronously(self, document_jobs, account_jobs):
        store = get_object_store()
        purged_documents = 0
        all_document_jobs = dict.fromkeys(
            [*document_jobs, *DocumentDeletionJob.objects.values_list("pk", flat=True)]
        )
        for job_id in all_document_jobs:
            result = purge_document_deletion(job_id, store)
            if result.outcome == DeletionOutcome.RETRY_SCHEDULED:
                raise CommandError("Synchronous restore deletion could not purge private object storage")
            purged_documents += int(result.outcome == DeletionOutcome.PURGED)
        purged_accounts = 0
        all_account_jobs = dict.fromkeys(
            [*account_jobs, *AccountDeletionJob.objects.values_list("pk", flat=True)]
        )
        for job_id in all_account_jobs:
            result = purge_account_deletion(job_id)
            if result.outcome == AccountDeletionOutcome.RETRY_SCHEDULED:
                raise CommandError("Synchronous restore account deletion still has dependent documents")
            purged_accounts += int(result.outcome == AccountDeletionOutcome.PURGED)
        return purged_documents, purged_accounts

    def handle(self, *args, **options):
        source = Path(options["input"]).expanduser().resolve()
        if not source.is_file():
            raise CommandError("The tombstone log does not exist")
        try:
            content = source.read_text(encoding="utf-8")
            entries = decode_tombstone_log(content)
            if options["synchronous"]:
                document_jobs, account_jobs, document_dispatch, account_dispatch = (
                    self._synchronous_dispatchers(options)
                )
            else:
                document_jobs = account_jobs = None
                document_dispatch = safe_enqueue_document_deletion
                account_dispatch = safe_enqueue_account_deletion
            result = replay_restore_tombstones(
                entries,
                document_dispatch=document_dispatch,
                account_dispatch=account_dispatch,
            )
            purged_documents = purged_accounts = 0
            if options["synchronous"]:
                purged_documents, purged_accounts = self._purge_synchronously(
                    document_jobs, account_jobs
                )
        except (OSError, InvalidTombstoneLog) as exc:
            raise CommandError("The tombstone log is invalid; restored traffic must remain closed") from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {result.imported}; hidden accounts {result.accounts_hidden}; "
                f"hidden documents {result.documents_hidden}; purged accounts {purged_accounts}; "
                f"purged documents {purged_documents}."
            )
        )
