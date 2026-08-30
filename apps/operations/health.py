from dataclasses import dataclass
import hashlib
import io

from django.core.cache import cache
from django.db import connection

from apps.documents.backends import get_object_store


@dataclass(frozen=True)
class Readiness:
    status: str
    http_status: int
    dependencies: dict


def _database_probe():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone() == (1,)


def _cache_probe():
    key = "phr:health:readiness"
    cache.set(key, "ok", timeout=10)
    healthy = cache.get(key) == "ok"
    cache.delete(key)
    return healthy


def _object_storage_probe():
    payload = b""
    store = get_object_store()
    staged = store.put_staging(
        io.BytesIO(payload),
        expected_size=0,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    store.delete(staged)
    return True


def readiness(*, probes=None):
    probes = probes or {
        "database": _database_probe,
        "cache": _cache_probe,
        "object_storage": _object_storage_probe,
    }
    dependencies = {}
    for name in ("database", "cache", "object_storage"):
        probe = probes.get(name)
        try:
            healthy = probe is not None and probe() is True
        except Exception:
            healthy = False
        dependencies[name] = "up" if healthy else "down"
    ready = all(value == "up" for value in dependencies.values())
    return Readiness("ready" if ready else "degraded", 200 if ready else 503, dependencies)
