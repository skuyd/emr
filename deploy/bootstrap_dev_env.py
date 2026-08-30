"""Create a private local .env from .env.example without ever replacing one."""

from __future__ import annotations

import secrets
import os
from pathlib import Path


_PLACEHOLDERS = {
    "change-me-before-deployment",
    "example-access-key",
    "example-secret-key",
}


def _replacement() -> str:
    return secrets.token_urlsafe(48)


def _generated_values(source: dict[str, str]) -> dict[str, str]:
    """Generate each credential once, then derive every dependent URL from it."""
    values = dict(source)
    values.update(
        {
            "DJANGO_SECRET_KEY": _replacement(),
            "ACCOUNTS_CRYPTO_SECRET": _replacement(),
            "POSTGRES_PASSWORD": _replacement(),
            "REDIS_PASSWORD": _replacement(),
            "MINIO_ROOT_USER": f"minio-{secrets.token_urlsafe(12)}",
            "MINIO_ROOT_PASSWORD": _replacement(),
        }
    )
    postgres_db = values.get("POSTGRES_DB", "familyphr_dev")
    postgres_user = values.get("POSTGRES_USER", "familyphr_dev")
    postgres_port = values.get("POSTGRES_PORT", "5432")
    redis_port = values.get("REDIS_PORT", "6379")
    minio_port = values.get("MINIO_API_PORT", "9000")
    bucket = values.get("MINIO_BUCKET", "familyphr-local")
    values.update(
        {
            "DATABASE_URL": f"postgresql://{postgres_user}:{values['POSTGRES_PASSWORD']}@127.0.0.1:{postgres_port}/{postgres_db}",
            "REDIS_URL": f"redis://:{values['REDIS_PASSWORD']}@127.0.0.1:{redis_port}/0",
            "CELERY_BROKER_URL": f"redis://:{values['REDIS_PASSWORD']}@127.0.0.1:{redis_port}/0",
            "CELERY_RESULT_BACKEND": f"redis://:{values['REDIS_PASSWORD']}@127.0.0.1:{redis_port}/1",
            "AWS_ACCESS_KEY_ID": values["MINIO_ROOT_USER"],
            "AWS_SECRET_ACCESS_KEY": values["MINIO_ROOT_PASSWORD"],
            "AWS_STORAGE_BUCKET_NAME": bucket,
            "AWS_S3_ENDPOINT_URL": f"http://127.0.0.1:{minio_port}",
        }
    )
    return values


def bootstrap(example_path: Path, env_path: Path) -> bool:
    """Create *env_path* from *example_path*, returning False when it exists."""
    example = example_path.read_text(encoding="utf-8")
    source = {}
    for line in example.splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            source[key] = value
    generated = _generated_values(source)

    output = []
    for line in example.splitlines(keepends=True):
        key, separator, value = line.partition("=")
        if separator and key in generated:
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            line = f"{key}={generated[key]}{ending}"
        elif separator and value.strip() in _PLACEHOLDERS:
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            line = f"{key}={_replacement()}{ending}"
        output.append(line)
    try:
        with env_path.open("x", encoding="utf-8", newline="") as handle:
            handle.write("".join(output))
    except FileExistsError:
        return False
    if os.name == "posix":
        os.chmod(env_path, 0o600)
    return True


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / ".env"
    if bootstrap(root / ".env.example", target):
        print("Created .env with fresh local development secrets.")
    else:
        print("Existing .env left unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
