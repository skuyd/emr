import os
from pathlib import Path
import subprocess
import sys

from deploy.bootstrap_production_env import bootstrap


ROOT = Path(__file__).resolve().parents[2]


def safe_production_environment():
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "APP_DOMAIN": "phr.example.test",
            "ACME_EMAIL": "operations@example.test",
            "DJANGO_SECRET_KEY": "prod-django-R8Lw1aQ9xP4vN7kT2mH6jC3sF5bY0uD8eG4zW9rK1pX7nV6q",
            "ACCOUNTS_CRYPTO_SECRET": "prod-accounts-v1-R8Lw1aQ9xP4vN7kT2mH6jC3sF5bY0uD8",
            "NOTIFICATIONS_CRYPTO_SECRET": "prod-notify-v1-Q4nT8mR2xL7vP5kD9sF1bY6uG3eW0zC8",
            "TOMBSTONE_HASH_KEY": "prod-tombstone-hash-v1-N7kT2mH6jC3sF5bY0uD8eG4zW9rK1pX7",
            "TOMBSTONE_SIGNING_KEY": "prod-tombstone-sign-v1-P4vN7kT2mH6jC3sF5bY0uD8eG4zW9rK1",
            "ANALYTICS_HASH_KEY": "prod-analytics-v1-T2mH6jC3sF5bY0uD8eG4zW9rK1pX7nV6",
            "AUDIT_HASH_KEY": "prod-audit-v1-H6jC3sF5bY0uD8eG4zW9rK1pX7nV6qL2",
            "OPERATIONS_METRICS_TOKEN": "prod-metrics-v1-C3sF5bY0uD8eG4zW9rK1pX7nV6qL2tH8",
            "DJANGO_ALLOWED_HOSTS": "phr.example.test",
            "CSRF_TRUSTED_ORIGINS": "https://phr.example.test",
            "SESSION_COOKIE_SECURE": "True",
            "CSRF_COOKIE_SECURE": "True",
            "SECURE_SSL_REDIRECT": "True",
            "SECURE_HSTS_SECONDS": "31536000",
            "DATABASE_URL": "postgresql://phr:synthetic-password@db.example.test:5432/phr",
            "DATABASE_SSL_REQUIRE": "True",
            "REDIS_URL": "rediss://redis.example.test:6379/0",
            "CELERY_BROKER_URL": "rediss://redis.example.test:6379/0",
            "CELERY_RESULT_BACKEND": "rediss://redis.example.test:6379/1",
            "OTP_PROVIDER": "https_gateway",
            "SMS_GATEWAY_URL": "https://sms.example.test/send",
            "SMS_GATEWAY_API_KEY": "prod-sms-api-key",
            "SMS_GATEWAY_SIGNING_SECRET": "prod-sms-signing-secret",
            "SMS_GATEWAY_TEMPLATE_ID": "login-v1",
            "SMS_GATEWAY_ALLOWED_HOSTS": "sms.example.test",
            "SMS_GATEWAY_TIMEOUT_SECONDS": "10",
            "DOCUMENT_STORAGE_BACKEND": "s3",
            "DOCUMENT_S3_BUCKET": "private-phr-bucket",
            "DOCUMENT_S3_ENDPOINT_URL": "https://s3.example.test",
            "DOCUMENT_S3_ALLOWED_HOSTS": "s3.example.test",
            "DOCUMENT_S3_REGION": "ap-southeast-1",
            "DOCUMENT_S3_ACCESS_KEY_ID": "prod-storage-access-key",
            "DOCUMENT_S3_SECRET_ACCESS_KEY": "prod-storage-secret-key",
            "DOCUMENT_S3_PREFIX": "closed-trial",
            "DOCUMENT_S3_ALLOW_INSECURE_INTERNAL": "False",
            "DOCUMENT_S3_REQUIRE_ENCRYPTION": "True",
            "PHR_OCR_PROVIDER": "paddle",
            "PHR_OCR_PADDLE_DETECTION_MODEL_DIR": "/models/detection",
            "PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR": "/models/recognition",
            "ALLOW_PERFORMANCE_SEED": "False",
        }
    )
    return environment


def test_production_bootstrap_generates_internal_secrets_and_never_overwrites(tmp_path):
    example = tmp_path / "example"
    destination = tmp_path / ".env.production"
    example.write_text(
        """DJANGO_SECRET_KEY=change-me-before-deployment
ACCOUNTS_CRYPTO_SECRET=change-me-before-deployment
NOTIFICATIONS_CRYPTO_SECRET=change-me-before-deployment
TOMBSTONE_HASH_KEY=change-me-before-deployment
TOMBSTONE_SIGNING_KEY=change-me-before-deployment
ANALYTICS_HASH_KEY=change-me-before-deployment
AUDIT_HASH_KEY=change-me-before-deployment
OPERATIONS_METRICS_TOKEN=change-me-before-deployment
POSTGRES_DB=familyphr
POSTGRES_USER=familyphr
POSTGRES_PASSWORD=change-me-before-deployment
DATABASE_URL=change-me-before-deployment
REDIS_PASSWORD=change-me-before-deployment
REDIS_URL=change-me-before-deployment
CELERY_BROKER_URL=change-me-before-deployment
CELERY_RESULT_BACKEND=change-me-before-deployment
MINIO_ROOT_USER=change-me-before-deployment
MINIO_ROOT_PASSWORD=change-me-before-deployment
DOCUMENT_S3_ACCESS_KEY_ID=change-me-before-deployment
DOCUMENT_S3_SECRET_ACCESS_KEY=change-me-before-deployment
SMS_GATEWAY_SIGNING_SECRET=change-me-before-deployment
SMS_GATEWAY_URL=https://required-sms.example/send
""",
        encoding="utf-8",
    )

    assert bootstrap(example, destination) is True
    values = dict(line.split("=", 1) for line in destination.read_text(encoding="utf-8").splitlines())
    assert all("change-me" not in value for value in values.values())
    assert values["DATABASE_URL"].startswith("postgresql://familyphr:")
    assert values["DATABASE_URL"].endswith("@postgres:5432/familyphr")
    assert values["REDIS_URL"] == values["CELERY_BROKER_URL"]
    assert values["CELERY_RESULT_BACKEND"].endswith("@redis:6379/1")
    assert values["DOCUMENT_S3_ACCESS_KEY_ID"] == values["MINIO_ROOT_USER"]
    assert values["DOCUMENT_S3_SECRET_ACCESS_KEY"] == values["MINIO_ROOT_PASSWORD"]
    assert values["SMS_GATEWAY_URL"] == "https://required-sms.example/send"

    destination.write_text("existing=preserved\n", encoding="utf-8")
    assert bootstrap(example, destination) is False
    assert destination.read_text(encoding="utf-8") == "existing=preserved\n"


def test_production_container_is_non_root_locked_and_precollects_static_assets():
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-prod.lock").read_text(encoding="utf-8")

    assert "FROM python:3.11.16-slim-bookworm" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "requirements-prod.lock" in dockerfile
    assert "collectstatic --noinput --skip-checks" in dockerfile
    assert "DJANGO_SETTINGS_MODULE=config.settings.build" in dockerfile
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "tests" in ignored
    assert "node_modules" in ignored and "test-results" in ignored
    for dependency in ("Django==", "gunicorn==", "whitenoise==", "paddleocr==", "paddlepaddle=="):
        assert dependency.lower() in requirements.lower()


def test_production_compose_exposes_only_tls_proxy_and_gates_app_on_migration_and_storage():
    compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    for service in ("postgres", "redis", "minio", "migrate", "web", "worker", "beat", "caddy"):
        assert f"  {service}:" in compose
    assert '"80:80"' in compose and '"443:443"' in compose
    assert "5432:5432" not in compose and "6379:6379" not in compose and "9000:9000" not in compose
    assert "read_only: true" in compose
    assert 'user: "10001:10001"' in compose
    assert "no-new-privileges:true" in compose
    assert "check --deploy --settings=config.settings.production" in compose
    assert "check_private_storage" in compose
    assert "service_completed_successfully" in compose
    assert "anonymous set none" in compose and "version enable" in compose
    assert 'profiles: ["closed-trial"]' in compose
    assert '"$$DOCUMENT_S3_ENDPOINT_URL"' in compose
    assert "log {" not in caddy
    assert "@private_operations path /admin /admin/* /internal /internal/*" in caddy
    assert "respond @private_operations 404" in caddy
    assert "health_uri /health/live/" in caddy
    assert "max_size 110MB" in caddy


def test_backup_restore_uses_encryption_newer_tombstones_and_isolated_synchronous_purge():
    backup = (ROOT / "deploy" / "backup.ps1").read_text(encoding="utf-8")
    restore = (ROOT / "deploy" / "restore.ps1").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")

    assert "age --recipient" in backup
    assert "manifest.sha256" in backup
    assert "backup-metadata.json" in backup
    assert "Refusing to overwrite an existing backup artifact" in backup
    assert "-not $backupCompleted" in backup
    assert "ReparsePoint" in backup
    assert "LatestTombstoneBackup" in restore
    assert "tombstoneBackupTime -lt $dataBackupTime" in restore
    assert "Get-VerifiedBackupMetadata" in restore
    assert "tombstoneMetadata.CreatedAt -lt $dataMetadata.CreatedAt" in restore
    assert "Copy-Item -LiteralPath $latestTombstones" in restore
    assert "tar -tzf" in restore and "tar -tvzf" in restore
    assert "expectedFiles.Add" in restore
    assert "unmanifested artifact" in restore
    assert "metadata identity" in restore
    assert 'ValidateSet("RESTORE-DRILL")' in restore
    assert "*_restore_drill" in restore and "restore-drill/" in restore
    assert "--synchronous --confirm ISOLATED-RESTORE-DRILL" in compose
    assert "test ! -s /tmp/existing-restore-prefix" in compose


def test_production_settings_fail_closed_without_external_configuration():
    environment = os.environ.copy()
    environment.pop("DJANGO_SECRET_KEY", None)
    environment["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
    completed = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--settings=config.settings.production"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    for error_id in ("phr.E002", "phr.E012", "phr.E013", "phr.E014", "phr.E015", "phr.E016"):
        assert error_id in combined


def test_fully_configured_production_settings_pass_django_deployment_checks():
    completed = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--settings=config.settings.production"],
        cwd=ROOT,
        env=safe_production_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined
    assert "phr.E" not in combined
