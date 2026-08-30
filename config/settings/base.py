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
        "digest": "cc5d9830175699f2176b742f56b260328618a7a0e0d2964bb715cb25dc216595",
        "content": "敏感信息仅用于您明确授权的资料整理与服务支持。",
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
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
