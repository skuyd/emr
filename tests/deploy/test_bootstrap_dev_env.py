from deploy.bootstrap_dev_env import bootstrap


def test_bootstrap_creates_randomized_env_without_overwriting_existing_file(tmp_path):
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("""DJANGO_SECRET_KEY=change-me-before-deployment
ACCOUNTS_CRYPTO_SECRET=change-me-before-deployment
DATABASE_URL=sqlite:///db.sqlite3
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
POSTGRES_DB=familyphr_dev
POSTGRES_USER=familyphr_dev
POSTGRES_PASSWORD=change-me-before-deployment
REDIS_PASSWORD=change-me-before-deployment
MINIO_ROOT_USER=familyphr_local_admin
MINIO_ROOT_PASSWORD=change-me-before-deployment
MINIO_BUCKET=familyphr-local
DOCUMENT_S3_ACCESS_KEY_ID=example-access-key
DOCUMENT_S3_SECRET_ACCESS_KEY=example-secret-key
DOCUMENT_S3_BUCKET=family-phr-example
DOCUMENT_S3_ENDPOINT_URL=http://127.0.0.1:9000
OTP_PROVIDER=console
""", encoding="utf-8")

    assert bootstrap(example, target) is True
    generated = target.read_text(encoding="utf-8")
    assert "change-me-before-deployment" not in generated
    values = dict(line.split("=", 1) for line in generated.splitlines() if "=" in line)
    assert values["OTP_PROVIDER"] == "console"
    assert values["DATABASE_URL"] == (
        f"postgresql://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}@127.0.0.1:5432/{values['POSTGRES_DB']}"
    )
    assert values["REDIS_URL"] == f"redis://:{values['REDIS_PASSWORD']}@127.0.0.1:6379/0"
    assert values["CELERY_BROKER_URL"] == values["REDIS_URL"]
    assert values["CELERY_RESULT_BACKEND"] == f"redis://:{values['REDIS_PASSWORD']}@127.0.0.1:6379/1"
    assert values["DOCUMENT_S3_ACCESS_KEY_ID"] == values["MINIO_ROOT_USER"]
    assert values["DOCUMENT_S3_SECRET_ACCESS_KEY"] == values["MINIO_ROOT_PASSWORD"]
    assert values["DOCUMENT_S3_BUCKET"] == values["MINIO_BUCKET"]

    target.write_text("existing=preserved\n", encoding="utf-8")
    assert bootstrap(example, target) is False
    assert target.read_text(encoding="utf-8") == "existing=preserved\n"


def test_bootstrap_does_not_overwrite_if_env_appears_during_creation(tmp_path, monkeypatch):
    example = tmp_path / ".env.example"
    target = tmp_path / ".env"
    example.write_text("DJANGO_SECRET_KEY=change-me-before-deployment\n", encoding="utf-8")
    target.write_text("created-by-another-process\n", encoding="utf-8")
    original_exists = type(target).exists

    monkeypatch.setattr(
        type(target), "exists", lambda path: False if path == target else original_exists(path)
    )

    assert bootstrap(example, target) is False
    assert target.read_text(encoding="utf-8") == "created-by-another-process\n"
