from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.tasks import safe_enqueue_account_deletion
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

    def handle(self, *args, **options):
        source = Path(options["input"]).expanduser().resolve()
        if not source.is_file():
            raise CommandError("The tombstone log does not exist")
        try:
            content = source.read_text(encoding="utf-8")
            entries = decode_tombstone_log(content)
            result = replay_restore_tombstones(
                entries,
                document_dispatch=safe_enqueue_document_deletion,
                account_dispatch=safe_enqueue_account_deletion,
            )
        except (OSError, InvalidTombstoneLog) as exc:
            raise CommandError("The tombstone log is invalid; restored traffic must remain closed") from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {result.imported}; hidden accounts {result.accounts_hidden}; "
                f"hidden documents {result.documents_hidden}."
            )
        )
