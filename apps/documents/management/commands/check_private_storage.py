from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.documents.backends import get_object_store
from apps.documents.storage import S3ObjectStore
from apps.documents.storage_checks import PrivateStorageCheckFailed, verify_private_s3_bucket


class Command(BaseCommand):
    help = "Fail unless the configured S3 bucket is private, versioned and suitably encrypted."

    def handle(self, *args, **options):
        try:
            store = get_object_store()
            if not isinstance(store, S3ObjectStore):
                raise PrivateStorageCheckFailed("s3_backend_required")
            verify_private_s3_bucket(
                store.client,
                store.bucket,
                require_encryption=getattr(settings, "DOCUMENT_S3_REQUIRE_ENCRYPTION", True),
                allow_missing_public_access_block=getattr(
                    settings, "DOCUMENT_S3_ALLOW_INSECURE_INTERNAL", False
                ),
            )
        except PrivateStorageCheckFailed as error:
            raise CommandError(f"Private object storage check failed: {error.code}") from None
        except Exception:
            raise CommandError("Private object storage check failed: storage_unavailable") from None
        self.stdout.write(self.style.SUCCESS("Private object storage checks passed."))
