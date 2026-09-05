from dataclasses import dataclass
from copy import deepcopy
import hashlib
import io
import math
from threading import Lock
from time import monotonic
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils.module_loading import import_string
from django_redis.pool import ConnectionFactory

from apps.documents.backends import get_object_store


@dataclass(frozen=True)
class Readiness:
    status: str
    http_status: int
    dependencies: dict


_DEPENDENCIES = ("database", "cache", "object_storage")
_readiness_lock = Lock()
_readiness_cache = None


def _bounded_setting(name, default, maximum):
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return max(0.1, min(value, maximum)) if math.isfinite(value) else default


def _probe_timeout():
    return _bounded_setting("OPERATIONS_READINESS_TIMEOUT_SECONDS", 1.0, 3.0)


class ReadinessRedisConnectionFactory(ConnectionFactory):
    def get_or_create_connection_pool(self, params):
        # django-redis normally shares pools process-wide by URL. A probe must
        # neither inherit application timeouts nor close application sockets.
        pool = self.get_connection_pool(params)
        pool.connection_kwargs.update({
            "socket_connect_timeout": self.options["SOCKET_CONNECT_TIMEOUT"],
            "socket_timeout": self.options["SOCKET_TIMEOUT"],
            "retry_on_timeout": False,
            "retry": self.options["CONNECTION_POOL_KWARGS"]["retry"],
        })
        return pool


def _database_probe():
    timeout = _probe_timeout()
    probe = connection.copy()
    options = probe.settings_dict.setdefault("OPTIONS", {})
    if connection.vendor == "postgresql":
        # libpq treats connect_timeout < 2 as 2 seconds. Keep probes out of the
        # application's pool and apply query limits at connection startup.
        options["connect_timeout"] = max(2, math.ceil(timeout))
        options["pool"] = False
        milliseconds = math.ceil(timeout * 1000)
        options["options"] = (
            f"{options.get('options', '')} -c statement_timeout={milliseconds}"
            f" -c lock_timeout={milliseconds}"
        ).strip()
    elif connection.vendor == "sqlite":
        options["timeout"] = timeout
    else:
        return False
    probe.settings_dict["CONN_MAX_AGE"] = 0
    try:
        with probe.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    finally:
        probe.close()


def _cache_probe():
    configuration = deepcopy(settings.CACHES["default"])
    backend = configuration["BACKEND"]
    dedicated = backend == "django_redis.cache.RedisCache"
    if dedicated:
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        timeout = _probe_timeout()
        options = configuration.setdefault("OPTIONS", {})
        options.update({
            "SOCKET_CONNECT_TIMEOUT": timeout,
            "SOCKET_TIMEOUT": timeout,
            "IGNORE_EXCEPTIONS": False,
            "CLOSE_CONNECTION": True,
            "CONNECTION_FACTORY": "apps.operations.health.ReadinessRedisConnectionFactory",
            "CONNECTION_POOL_CLASS": "redis.connection.ConnectionPool",
        })
        pool_options = options.setdefault("CONNECTION_POOL_KWARGS", {})
        pool_options.update({"retry_on_timeout": False, "retry": Retry(NoBackoff(), 0)})
        probe_cache = import_string(backend)(configuration["LOCATION"], configuration)
    elif backend == "django.core.cache.backends.locmem.LocMemCache":
        probe_cache = cache
    else:
        # Do not run a network backend without an explicit bounded adapter.
        return False
    key = f"phr:health:readiness:{uuid.uuid4().hex}"
    try:
        probe_cache.set(key, "ok", timeout=10)
        return probe_cache.get(key) == "ok"
    finally:
        try:
            probe_cache.delete(key)
        finally:
            if dedicated:
                probe_cache.close()


def _object_storage_probe():
    timeout = _probe_timeout()
    store = get_object_store(connect_timeout=timeout, read_timeout=timeout, max_attempts=1)
    if settings.DOCUMENT_STORAGE_BACKEND.casefold() == "s3":
        # Readiness checks bucket connectivity, not upload/delete permissions.
        # The release storage gate verifies the complete write/delete contract.
        try:
            store.client.head_bucket(Bucket=store.bucket)
            return True
        finally:
            store.client.close()
    payload = b""
    staged = store.put_staging(
        io.BytesIO(payload),
        expected_size=0,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    store.delete(staged)
    return True


def _run_probes(probes):
    dependencies = {}
    for name in _DEPENDENCIES:
        probe = probes.get(name)
        try:
            healthy = probe is not None and probe() is True
        except Exception:
            healthy = False
        dependencies[name] = "up" if healthy else "down"
    ready = all(value == "up" for value in dependencies.values())
    return Readiness("ready" if ready else "degraded", 200 if ready else 503, dependencies)


def readiness(*, probes=None):
    if probes is not None:
        return _run_probes(probes)
    global _readiness_cache
    cached = _readiness_cache
    if cached is not None and monotonic() < cached[0]:
        return cached[1]
    if not _readiness_lock.acquire(blocking=False):
        return Readiness("checking", 503, {name: "unknown" for name in _DEPENDENCIES})
    try:
        cached = _readiness_cache
        if cached is not None and monotonic() < cached[0]:
            return cached[1]
        result = _run_probes({
            "database": _database_probe,
            "cache": _cache_probe,
            "object_storage": _object_storage_probe,
        })
        ttl = _bounded_setting("OPERATIONS_READINESS_CACHE_SECONDS", 2.0, 5.0)
        _readiness_cache = (monotonic() + ttl, result)
        return result
    finally:
        _readiness_lock.release()
