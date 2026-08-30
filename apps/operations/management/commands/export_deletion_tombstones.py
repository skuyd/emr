import os
from pathlib import Path
import tempfile

from django.core.management.base import BaseCommand, CommandError

from apps.operations.tombstones import (
    InvalidTombstoneLog,
    decode_tombstone_log,
    encode_tombstone_log,
    export_tombstone_entries,
    merge_tombstone_entries,
)


class Command(BaseCommand):
    help = "Merge signed deletion tombstones into an external restore log."

    def add_arguments(self, parser):
        parser.add_argument("output", help="Explicit JSONL tombstone log path")

    def handle(self, *args, **options):
        output = Path(options["output"]).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = decode_tombstone_log(output.read_text(encoding="utf-8")) if output.exists() else []
            merged = merge_tombstone_entries(existing, export_tombstone_entries())
            content = encode_tombstone_log(merged)
        except (OSError, InvalidTombstoneLog) as exc:
            raise CommandError("The external tombstone log could not be validated") from exc
        temporary_name = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=output.parent,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_name, output)
        except OSError as exc:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise CommandError("The external tombstone log could not be written") from exc
        self.stdout.write(self.style.SUCCESS(f"Exported {len(merged)} signed deletion tombstones."))
