from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_pins_local_services_and_keeps_ports_on_loopback():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "postgres:18.6-alpine3.24" in compose
    assert "redis:7.2.16-alpine3.21" in compose
    assert "minio/minio:RELEASE.2025-09-07T16-13-09Z" in compose
    assert "minio/mc:RELEASE.2025-08-13T08-35-41Z" in compose
    assert "/var/lib/postgresql" in compose
    assert "127.0.0.1:${POSTGRES_PORT:?POSTGRES_PORT must be set}:5432" in compose
    assert "127.0.0.1:${REDIS_PORT:?REDIS_PORT must be set}:6379" in compose
    assert "127.0.0.1:${MINIO_API_PORT:?MINIO_API_PORT must be set}:9000" in compose
    assert "127.0.0.1:${MINIO_CONSOLE_PORT:?MINIO_CONSOLE_PORT must be set}:9001" in compose
    assert "healthcheck:" in compose
    assert "postgres_data:" in compose
    assert "redis_data:" in compose
    assert "minio_data:" in compose
    assert "0.0.0.0" not in compose


def test_compose_requires_env_credentials_and_initializes_private_versioned_bucket():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    init_sql = (ROOT / "deploy" / "docker" / "postgres" / "init.sql").read_text(encoding="utf-8")

    for variable in ("POSTGRES_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MINIO_BUCKET"):
        assert f"${{{variable}:?{variable} must be set}}" in compose
    assert "${REDIS_PASSWORD:?REDIS_PASSWORD must be set}" in compose
    assert "redis-server --appendonly yes --requirepass" in compose
    assert "REDISCLI_AUTH=$$REDIS_PASSWORD redis-cli ping" in compose
    assert "mc mb --ignore-existing" in compose
    assert "mc version enable" in compose
    assert "anonymous set" not in compose
    assert "policy set" not in compose
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm;" in init_sql
