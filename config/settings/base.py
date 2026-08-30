import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="unsafe-development-key-change-before-deployment")
ACCOUNTS_CRYPTO_SECRET = env("ACCOUNTS_CRYPTO_SECRET", default=SECRET_KEY)
ACCOUNTS_CRYPTO_SECRET_CONFIGURED = "ACCOUNTS_CRYPTO_SECRET" in os.environ
DEBUG = env.bool("DJANGO_DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts.apps.AccountsConfig",
    "apps.patients.apps.PatientsConfig",
    "apps.documents.apps.DocumentsConfig",
    "apps.processing.apps.ProcessingConfig",
    "apps.core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.session.SessionExpiryMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "accounts.Account"
LOGIN_URL = "/login/"

CONSENT_POLICIES = {
    "privacy": {
        "version": "2026-08-30",
        "digest": "7d9fba7a27aa58a5ee1eaa765b882bef5df9831c7032a00324c9441c81809e2d",
        "content": "我们会在提供服务所必需的范围内处理您主动提交的信息，并采取措施保护隐私。",
    },
    "sensitive_data": {
        "version": "2026-08-30",
        "digest": "ab90c5b1c913f25573ff94c13609e7d2d76162c4efea78f9bc4c06ced16fdedf",
        "content": "敏感个人信息处理规则：敏感个人信息仅用于您明确授权的资料整理与服务支持。",
    },
    "upload_authority": {
        "version": "2026-08-30",
        "digest": "16cea6d1a6885005c950168f249c6a5f1f2f0b305764c375eec8ec02f2bf5be7",
        "content": "我确认有权上传并管理相关资料。",
    },
}

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Originals are never served from MEDIA_URL. The local adapter is private and
# signs a short-lived application URL; production can switch to a private S3
# compatible bucket through environment configuration.
DOCUMENT_STORAGE_BACKEND = env("DOCUMENT_STORAGE_BACKEND", default="local")
DOCUMENT_STORAGE_ROOT = Path(env("DOCUMENT_STORAGE_ROOT", default=str(BASE_DIR / ".runtime" / "objects")))
DOCUMENT_S3_BUCKET = env("DOCUMENT_S3_BUCKET", default="")
DOCUMENT_S3_ENDPOINT_URL = env("DOCUMENT_S3_ENDPOINT_URL", default="")
DOCUMENT_S3_REGION = env("DOCUMENT_S3_REGION", default="us-east-1")
DOCUMENT_S3_ACCESS_KEY_ID = env("DOCUMENT_S3_ACCESS_KEY_ID", default="")
DOCUMENT_S3_SECRET_ACCESS_KEY = env("DOCUMENT_S3_SECRET_ACCESS_KEY", default="")
DOCUMENT_S3_PREFIX = env("DOCUMENT_S3_PREFIX", default="")
DOCUMENT_UPLOAD_PATIENT_REQUESTS_PER_MINUTE = env.int("DOCUMENT_UPLOAD_PATIENT_REQUESTS_PER_MINUTE", default=120)
DOCUMENT_UPLOAD_IP_REQUESTS_PER_MINUTE = env.int("DOCUMENT_UPLOAD_IP_REQUESTS_PER_MINUTE", default=300)
PROCESSING_PIPELINE_FACTORY = env("PROCESSING_PIPELINE_FACTORY", default="")
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BEAT_SCHEDULE = {
    "recover-stale-processing-runs": {
        "task": "processing.recover_stale_runs",
        "schedule": 60.0,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
