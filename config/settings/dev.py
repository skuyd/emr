import os
import secrets
from pathlib import Path

import environ

environ.Env.read_env(Path(__file__).resolve().parent.parent.parent / ".env")

from .base import *  # noqa: F403


def _development_secret_key(inherited_secret_key, *, configured):
    if (
        inherited_secret_key == "unsafe-development-key-change-before-deployment"
        and not configured
    ):
        return secrets.token_urlsafe(48)
    return inherited_secret_key


SECRET_KEY = _development_secret_key(  # noqa: F405
    SECRET_KEY, configured="DJANGO_SECRET_KEY" in os.environ
)
ACCOUNTS_CRYPTO_SECRET = env("ACCOUNTS_CRYPTO_SECRET", default=SECRET_KEY)  # noqa: F405
NOTIFICATIONS_CRYPTO_SECRET = env("NOTIFICATIONS_CRYPTO_SECRET", default=ACCOUNTS_CRYPTO_SECRET)  # noqa: F405
TOMBSTONE_HASH_KEY = env("TOMBSTONE_HASH_KEY", default=SECRET_KEY)  # noqa: F405
TOMBSTONE_SIGNING_KEY = env("TOMBSTONE_SIGNING_KEY", default=SECRET_KEY)  # noqa: F405
ANALYTICS_HASH_KEY = env("ANALYTICS_HASH_KEY", default=SECRET_KEY)  # noqa: F405
AUDIT_HASH_KEY = env("AUDIT_HASH_KEY", default=SECRET_KEY)  # noqa: F405
DEBUG = env.bool("DJANGO_DEBUG", default=True)  # noqa: F405
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)  # noqa: F405
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)  # noqa: F405
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)  # noqa: F405
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)  # noqa: F405
DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///db.sqlite3"),  # noqa: F405
}
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # Acquire the write lock before reading so concurrent uploads can wait rather
    # than fail immediately when upgrading a deferred transaction to a writer.
    sqlite_options = DATABASES["default"].setdefault("OPTIONS", {})
    sqlite_options.setdefault("transaction_mode", "IMMEDIATE")
    sqlite_options.setdefault("timeout", 30)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
OTP_PROVIDER = env("OTP_PROVIDER", default="development")  # noqa: F405
OTP_FIXED_CODE = env("OTP_FIXED_CODE", default="230412")  # noqa: F405

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")  # noqa: F405
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")  # noqa: F405
