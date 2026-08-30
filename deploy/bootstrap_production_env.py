"""Create a closed-trial production environment without overwriting one."""

from __future__ import annotations

import os
from pathlib import Path
import secrets


def _secret():
    return secrets.token_urlsafe(48)


def _values(source):
    values = dict(source)
    for key in (
        "DJANGO_SECRET_KEY",
        "ACCOUNTS_CRYPTO_SECRET",
        "NOTIFICATIONS_CRYPTO_SECRET",
        "TOMBSTONE_HASH_KEY",
        "TOMBSTONE_SIGNING_KEY",
        "ANALYTICS_HASH_KEY",
        "AUDIT_HASH_KEY",
        "OPERATIONS_METRICS_TOKEN",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "SMS_GATEWAY_SIGNING_SECRET",
    ):
        values[key] = _secret()
    values["MINIO_ROOT_USER"] = f"phr-{secrets.token_urlsafe(12)}"
    values["DOCUMENT_S3_ACCESS_KEY_ID"] = values["MINIO_ROOT_USER"]
    values["DOCUMENT_S3_SECRET_ACCESS_KEY"] = values["MINIO_ROOT_PASSWORD"]
    database = values.get("POSTGRES_DB", "familyphr")
    user = values.get("POSTGRES_USER", "familyphr")
    values["DATABASE_URL"] = (
        f"postgresql://{user}:{values['POSTGRES_PASSWORD']}@postgres:5432/{database}"
    )
    values["REDIS_URL"] = f"redis://:{values['REDIS_PASSWORD']}@redis:6379/0"
    values["CELERY_BROKER_URL"] = values["REDIS_URL"]
    values["CELERY_RESULT_BACKEND"] = f"redis://:{values['REDIS_PASSWORD']}@redis:6379/1"
    return values


def bootstrap(example_path, destination):
    example_path = Path(example_path)
    destination = Path(destination)
    source_text = example_path.read_text(encoding="utf-8")
    source = {}
    for line in source_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            source[key] = value
    generated = _values(source)
    output = []
    for line in source_text.splitlines(keepends=True):
        key, separator, _value = line.partition("=")
        if separator and key in generated:
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            line = f"{key}={generated[key]}{ending}"
        output.append(line)
    try:
        with destination.open("x", encoding="utf-8", newline="") as target:
            target.write("".join(output))
    except FileExistsError:
        return False
    if os.name == "posix":
        os.chmod(destination, 0o600)
    return True


def main():
    root = Path(__file__).resolve().parents[1]
    created = bootstrap(
        root / "deploy" / ".env.production.example",
        root / ".env.production",
    )
    print(
        "Created .env.production; fill every remaining required-* external value."
        if created
        else "Existing .env.production left unchanged."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
