from .base import *  # noqa: F403

DEBUG = env.bool("DJANGO_DEBUG", default=True)  # noqa: F405
DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///db.sqlite3"),  # noqa: F405
}
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
OTP_DELIVERY_BACKEND = env("OTP_DELIVERY_BACKEND", default="console")  # noqa: F405

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")  # noqa: F405
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")  # noqa: F405
