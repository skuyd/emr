import os

from .base import *  # noqa: F403


PRODUCTION_DEPLOYMENT = True
DEBUG = False
OTP_FIXED_CODE = None
APP_DOMAIN = env("APP_DOMAIN", default="")  # noqa: F405
ACME_EMAIL = env("ACME_EMAIL", default="")  # noqa: F405

SECRET_KEY = env("DJANGO_SECRET_KEY", default=SECRET_KEY)  # noqa: F405
ACCOUNTS_CRYPTO_SECRET = env("ACCOUNTS_CRYPTO_SECRET", default="")  # noqa: F405
NOTIFICATIONS_CRYPTO_SECRET = env("NOTIFICATIONS_CRYPTO_SECRET", default="")  # noqa: F405
TOMBSTONE_HASH_KEY = env("TOMBSTONE_HASH_KEY", default="")  # noqa: F405
TOMBSTONE_SIGNING_KEY = env("TOMBSTONE_SIGNING_KEY", default="")  # noqa: F405
ANALYTICS_HASH_KEY = env("ANALYTICS_HASH_KEY", default="")  # noqa: F405
AUDIT_HASH_KEY = env("AUDIT_HASH_KEY", default="")  # noqa: F405
ACCOUNTS_CRYPTO_SECRET_CONFIGURED = bool(os.environ.get("ACCOUNTS_CRYPTO_SECRET"))
NOTIFICATIONS_CRYPTO_SECRET_CONFIGURED = bool(os.environ.get("NOTIFICATIONS_CRYPTO_SECRET"))
TOMBSTONE_HASH_KEY_CONFIGURED = bool(os.environ.get("TOMBSTONE_HASH_KEY"))
TOMBSTONE_SIGNING_KEY_CONFIGURED = bool(os.environ.get("TOMBSTONE_SIGNING_KEY"))
ANALYTICS_HASH_KEY_CONFIGURED = bool(os.environ.get("ANALYTICS_HASH_KEY"))
AUDIT_HASH_KEY_CONFIGURED = bool(os.environ.get("AUDIT_HASH_KEY"))
OPERATIONS_METRICS_TOKEN_CONFIGURED = bool(os.environ.get("OPERATIONS_METRICS_TOKEN"))

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])  # noqa: F405
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])  # noqa: F405
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)  # noqa: F405
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)  # noqa: F405
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)  # noqa: F405
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)  # noqa: F405
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///production-misconfigured.sqlite3"),  # noqa: F405
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DATABASE_CONN_MAX_AGE", default=60)  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
if env.bool("DATABASE_SSL_REQUIRE", default=True):  # noqa: F405
    DATABASES["default"].setdefault("OPTIONS", {})["sslmode"] = "require"

REDIS_URL = env("REDIS_URL", default="")  # noqa: F405
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            "KEY_PREFIX": "family-phr",
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="")  # noqa: F405
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="")  # noqa: F405
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_ALWAYS_EAGER = False

OTP_PROVIDER = env("OTP_PROVIDER", default="")  # noqa: F405

DOCUMENT_STORAGE_BACKEND = env("DOCUMENT_STORAGE_BACKEND", default="")  # noqa: F405
DOCUMENT_S3_BUCKET = env("DOCUMENT_S3_BUCKET", default="")  # noqa: F405
DOCUMENT_S3_ENDPOINT_URL = env("DOCUMENT_S3_ENDPOINT_URL", default="")  # noqa: F405
DOCUMENT_S3_REGION = env("DOCUMENT_S3_REGION", default="")  # noqa: F405
DOCUMENT_S3_ACCESS_KEY_ID = env("DOCUMENT_S3_ACCESS_KEY_ID", default="")  # noqa: F405
DOCUMENT_S3_SECRET_ACCESS_KEY = env("DOCUMENT_S3_SECRET_ACCESS_KEY", default="")  # noqa: F405
DOCUMENT_S3_PREFIX = env("DOCUMENT_S3_PREFIX", default="")  # noqa: F405
DOCUMENT_S3_ALLOWED_HOSTS = env.list("DOCUMENT_S3_ALLOWED_HOSTS", default=[])  # noqa: F405
DOCUMENT_S3_ALLOW_INSECURE_INTERNAL = env.bool(  # noqa: F405
    "DOCUMENT_S3_ALLOW_INSECURE_INTERNAL", default=False
)
DOCUMENT_S3_REQUIRE_ENCRYPTION = env.bool("DOCUMENT_S3_REQUIRE_ENCRYPTION", default=True)  # noqa: F405

MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MAX_AGE = 31536000
WHITENOISE_ALLOW_ALL_ORIGINS = False
