import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .storage import LocalObjectStore, S3ObjectStore


_S3_PREFIX_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,63})*"
)


def valid_s3_prefix(prefix, *, allow_empty=False):
    if prefix == "":
        return allow_empty
    return (
        isinstance(prefix, str)
        and len(prefix) <= 512
        and _S3_PREFIX_PATTERN.fullmatch(prefix) is not None
    )


def valid_s3_endpoint(endpoint, allowed_hosts, *, allow_insecure=False):
    if not endpoint:
        return True
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower().rstrip(".")
        parsed.port
    except (TypeError, ValueError):
        return False
    allowed = {
        value.lower().rstrip(".")
        for value in allowed_hosts
        if isinstance(value, str) and value and value != "*"
    }
    return (
        bool(host)
        and host in allowed
        and parsed.scheme in ({"https", "http"} if allow_insecure else {"https"})
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )


def get_object_store(*, connect_timeout=None, read_timeout=None, max_attempts=None):
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
        if not valid_s3_prefix(settings.DOCUMENT_S3_PREFIX, allow_empty=True):
            raise ImproperlyConfigured("Private object storage prefix is invalid")
        if not valid_s3_endpoint(
            settings.DOCUMENT_S3_ENDPOINT_URL,
            getattr(settings, "DOCUMENT_S3_ALLOWED_HOSTS", ()),
            allow_insecure=getattr(settings, "DOCUMENT_S3_ALLOW_INSECURE_INTERNAL", False),
        ):
            raise ImproperlyConfigured("Private object storage endpoint is not allowlisted")
        import boto3
        from botocore.config import Config

        client_options = {}
        config_options = {}
        if connect_timeout is not None:
            config_options["connect_timeout"] = connect_timeout
        if read_timeout is not None:
            config_options["read_timeout"] = read_timeout
        if max_attempts is not None:
            config_options["retries"] = {"total_max_attempts": max_attempts, "mode": "standard"}
        if config_options:
            client_options["config"] = Config(**config_options)

        client = boto3.client(
            "s3",
            endpoint_url=settings.DOCUMENT_S3_ENDPOINT_URL or None,
            region_name=settings.DOCUMENT_S3_REGION,
            aws_access_key_id=settings.DOCUMENT_S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.DOCUMENT_S3_SECRET_ACCESS_KEY,
            **client_options,
        )
        return S3ObjectStore(client, settings.DOCUMENT_S3_BUCKET, prefix=settings.DOCUMENT_S3_PREFIX)
    raise ImproperlyConfigured("Unknown private object storage backend")
