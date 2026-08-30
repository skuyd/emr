from django.core.exceptions import ImproperlyConfigured
import pytest

from apps.documents.backends import get_object_store, valid_s3_endpoint, valid_s3_prefix


@pytest.mark.parametrize(
    "endpoint,allowed,allow_insecure,expected",
    [
        ("", [], False, True),
        ("https://s3.example.test", ["s3.example.test"], False, True),
        ("http://minio:9000", ["minio"], True, True),
        ("http://minio:9000", ["minio"], False, False),
        ("https://metadata.internal", ["s3.example.test"], False, False),
        ("https://user:pass@s3.example.test", ["s3.example.test"], False, False),
        ("https://s3.example.test/?query=1", ["s3.example.test"], False, False),
        ("https://s3.example.test/path", ["s3.example.test"], False, False),
        ("https://s3.example.test:invalid", ["s3.example.test"], False, False),
    ],
)
def test_s3_endpoint_requires_exact_allowlist_and_safe_url(endpoint, allowed, allow_insecure, expected):
    assert valid_s3_endpoint(endpoint, allowed, allow_insecure=allow_insecure) is expected


@pytest.mark.parametrize(
    "prefix,allow_empty,expected",
    [
        ("tenant-a", False, True),
        ("restore-drill/202608", False, True),
        ("", True, True),
        ("", False, False),
        ("/tenant-a", False, False),
        ("tenant-a/", False, False),
        ("tenant-a//objects", False, False),
        ("tenant-a/../production", False, False),
        ("tenant-a\\objects", False, False),
        ("租户", False, False),
    ],
)
def test_s3_prefix_is_explicit_bounded_and_path_safe(prefix, allow_empty, expected):
    assert valid_s3_prefix(prefix, allow_empty=allow_empty) is expected


def test_s3_backend_rejects_unallowlisted_endpoint_before_creating_network_client(settings):
    settings.DOCUMENT_STORAGE_BACKEND = "s3"
    settings.DOCUMENT_S3_BUCKET = "private-bucket"
    settings.DOCUMENT_S3_ACCESS_KEY_ID = "access"
    settings.DOCUMENT_S3_SECRET_ACCESS_KEY = "secret"
    settings.DOCUMENT_S3_PREFIX = "tenant-a"
    settings.DOCUMENT_S3_ENDPOINT_URL = "https://metadata.internal"
    settings.DOCUMENT_S3_ALLOWED_HOSTS = ["s3.example.test"]
    settings.DOCUMENT_S3_ALLOW_INSECURE_INTERNAL = False

    with pytest.raises(ImproperlyConfigured, match="allowlisted"):
        get_object_store()


def test_s3_backend_rejects_unsafe_prefix_before_creating_network_client(settings):
    settings.DOCUMENT_STORAGE_BACKEND = "s3"
    settings.DOCUMENT_S3_BUCKET = "private-bucket"
    settings.DOCUMENT_S3_ACCESS_KEY_ID = "access"
    settings.DOCUMENT_S3_SECRET_ACCESS_KEY = "secret"
    settings.DOCUMENT_S3_PREFIX = "../production"
    settings.DOCUMENT_S3_ENDPOINT_URL = "https://s3.example.test"
    settings.DOCUMENT_S3_ALLOWED_HOSTS = ["s3.example.test"]
    settings.DOCUMENT_S3_ALLOW_INSECURE_INTERNAL = False

    with pytest.raises(ImproperlyConfigured, match="prefix"):
        get_object_store()
