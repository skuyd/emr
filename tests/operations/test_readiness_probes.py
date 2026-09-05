from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

import pytest

from apps.operations import health


def _mock_default_probes(monkeypatch, database=lambda: True):
    monkeypatch.setattr(health, "_database_probe", database)
    monkeypatch.setattr(health, "_cache_probe", lambda: True)
    monkeypatch.setattr(health, "_object_storage_probe", lambda: True)
    monkeypatch.setattr(health, "_readiness_cache", None, raising=False)


def test_readiness_caches_success_and_failure_for_only_a_short_window(monkeypatch, settings):
    settings.OPERATIONS_READINESS_CACHE_SECONDS = 2
    now = [100.0]
    calls = []
    _mock_default_probes(monkeypatch, lambda: calls.append(True) or len(calls) == 1)
    monkeypatch.setattr(health, "monotonic", lambda: now[0], raising=False)

    assert health.readiness().http_status == 200
    assert health.readiness().http_status == 200
    assert len(calls) == 1
    now[0] += 2.1
    assert health.readiness().http_status == 503
    assert health.readiness().http_status == 503
    assert len(calls) == 2


def test_parallel_readiness_requests_do_not_duplicate_slow_probes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def database():
        calls.append(True)
        entered.set()
        assert release.wait(timeout=5)
        return True

    _mock_default_probes(monkeypatch, database)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(health.readiness)
        assert entered.wait(timeout=2)
        second = executor.submit(health.readiness)
        try:
            # A refresh already in progress returns a bounded unavailable result,
            # rather than queueing another set of remote requests or waiting.
            assert second.result(timeout=2).http_status == 503
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=2).http_status == 200
    assert health.readiness().http_status == 200
    assert len(calls) == 1


@pytest.mark.parametrize("timeout", [0.25, 999])
def test_postgres_probe_uses_separate_connection_with_short_connect_and_query_timeouts(
    monkeypatch, settings, timeout,
):
    settings.OPERATIONS_READINESS_TIMEOUT_SECONDS = timeout
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = (1,)
    probe = SimpleNamespace(
        settings_dict={"OPTIONS": {"options": "-c application_name=phr", "pool": True}},
        cursor=lambda: cursor,
        close=MagicMock(),
    )
    existing_settings = deepcopy(probe.settings_dict)
    existing = SimpleNamespace(
        vendor="postgresql", settings_dict=existing_settings,
        copy=lambda: probe, cursor=lambda: cursor,
    )
    monkeypatch.setattr(health, "connection", existing)

    assert health._database_probe() is True

    options = probe.settings_dict["OPTIONS"]
    assert 2 <= options["connect_timeout"] <= 3
    assert "statement_timeout=" in options["options"]
    assert f"statement_timeout={250 if timeout == 0.25 else 3000}" in options["options"]
    assert options.get("pool") is not True
    assert existing.settings_dict == existing_settings
    cursor.execute.assert_called_once_with("SELECT 1")
    probe.close.assert_called_once()


def test_redis_probe_uses_dedicated_short_timeouts_no_retries_and_closes(monkeypatch, settings):
    settings.OPERATIONS_READINESS_TIMEOUT_SECONDS = 0.25
    settings.CACHES = {"default": {
        "BACKEND": "django_redis.cache.RedisCache", "LOCATION": "redis://cache.test:6379/0",
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }}
    captured = []
    cache = MagicMock()
    cache.get.return_value = "ok"

    def factory(location, configuration):
        captured.append((location, configuration))
        return cache

    monkeypatch.setattr(health, "cache", cache)
    monkeypatch.setattr(health, "import_string", lambda path: factory, raising=False)

    assert health._cache_probe() is True

    assert len(captured) == 1
    options = captured[0][1]["OPTIONS"]
    assert options["SOCKET_CONNECT_TIMEOUT"] == 0.25
    assert options["SOCKET_TIMEOUT"] == 0.25
    assert options["CONNECTION_POOL_KWARGS"]["retry_on_timeout"] is False
    assert options["CONNECTION_POOL_KWARGS"]["retry"]._retries == 0
    assert options["CLOSE_CONNECTION"] is True
    cache.close.assert_called_once()
    assert "SOCKET_TIMEOUT" not in settings.CACHES["default"]["OPTIONS"]


def test_s3_readiness_is_read_only_and_uses_bounded_nonretrying_client(monkeypatch, settings):
    settings.DOCUMENT_STORAGE_BACKEND = "s3"
    settings.OPERATIONS_READINESS_TIMEOUT_SECONDS = 0.25
    client = MagicMock()
    store = SimpleNamespace(client=client, bucket="synthetic-bucket")
    factory = MagicMock(return_value=store)
    monkeypatch.setattr(health, "get_object_store", factory)

    assert health._object_storage_probe() is True

    factory.assert_called_once_with(connect_timeout=0.25, read_timeout=0.25, max_attempts=1)
    client.head_bucket.assert_called_once_with(Bucket="synthetic-bucket")
    client.close.assert_called_once()
    client.put_object.assert_not_called()
    client.delete_object.assert_not_called()


def test_probe_timeouts_reach_the_actual_s3_client_factory(monkeypatch, settings):
    from apps.documents.backends import get_object_store

    settings.DOCUMENT_STORAGE_BACKEND = "s3"
    settings.DOCUMENT_S3_BUCKET = "synthetic-bucket"
    settings.DOCUMENT_S3_ACCESS_KEY_ID = "synthetic-access"
    settings.DOCUMENT_S3_SECRET_ACCESS_KEY = "synthetic-secret"
    settings.DOCUMENT_S3_PREFIX = ""
    settings.DOCUMENT_S3_ENDPOINT_URL = "https://storage.test"
    settings.DOCUMENT_S3_ALLOWED_HOSTS = ["storage.test"]
    factory = MagicMock()
    monkeypatch.setattr("boto3.client", factory)

    get_object_store(connect_timeout=0.25, read_timeout=0.5, max_attempts=1)

    config = factory.call_args.kwargs["config"]
    assert config.connect_timeout == 0.25
    assert config.read_timeout == 0.5
    assert config.retries["total_max_attempts"] == 1


def test_redis_probe_cannot_reuse_application_pool_or_url_timeout_overrides(monkeypatch, settings):
    from django_redis.pool import ConnectionFactory
    from redis import Redis

    settings.OPERATIONS_READINESS_TIMEOUT_SECONDS = 0.25
    location = "redis://cache.test:6379/0?socket_timeout=90&socket_connect_timeout=90"
    settings.CACHES = {"default": {
        "BACKEND": "django_redis.cache.RedisCache", "LOCATION": location,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }}
    poisoned_pool = MagicMock()
    monkeypatch.setattr(ConnectionFactory, "_pools", {location: poisoned_pool})
    observed = []
    stored = {}

    def command(client, *args, **kwargs):
        observed.append(client.connection_pool)
        if args[0] == "SET":
            stored[args[1]] = args[2]
        if args[0] == "GET":
            return stored[args[1]]
        return True

    # Real backend/factory/pool construction, with only transport commands
    # replaced so no DNS, sockets or production Redis instances are touched.
    monkeypatch.setattr(Redis, "execute_command", command)

    assert health._cache_probe() is True

    assert observed and all(pool is not poisoned_pool for pool in observed)
    poisoned_pool.disconnect.assert_not_called()
    parameters = observed[0].connection_kwargs
    assert parameters["socket_timeout"] == 0.25
    assert parameters["socket_connect_timeout"] == 0.25
    assert parameters["retry"]._retries == 0
