from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .storage import LocalObjectStore, S3ObjectStore


def get_object_store():
    backend = settings.DOCUMENT_STORAGE_BACKEND.casefold()
    if backend == "local":
        return LocalObjectStore(
            settings.DOCUMENT_STORAGE_ROOT,
            signing_key=settings.SECRET_KEY,
            base_url="/private-objects",
        )
    if backend == "s3":
        required = {
            "bucket": settings.DOCUMENT_S3_BUCKET,
            "access_key": settings.DOCUMENT_S3_ACCESS_KEY_ID,
            "secret_key": settings.DOCUMENT_S3_SECRET_ACCESS_KEY,
        }
        if not all(required.values()):
            raise ImproperlyConfigured("Private object storage is not configured")
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=settings.DOCUMENT_S3_ENDPOINT_URL or None,
            region_name=settings.DOCUMENT_S3_REGION,
            aws_access_key_id=settings.DOCUMENT_S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.DOCUMENT_S3_SECRET_ACCESS_KEY,
        )
        return S3ObjectStore(client, settings.DOCUMENT_S3_BUCKET, prefix=settings.DOCUMENT_S3_PREFIX)
    raise ImproperlyConfigured("Unknown private object storage backend")
