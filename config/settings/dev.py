import os
import secrets

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
DEBUG = env.bool("DJANGO_DEBUG", default=True)  # noqa: F405
DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///db.sqlite3"),  # noqa: F405
}
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
OTP_PROVIDER = env("OTP_PROVIDER", default="console")  # noqa: F405

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")  # noqa: F405
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")  # noqa: F405
