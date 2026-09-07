"""Single-host experience with synthetic data; never a production profile."""

import ipaddress

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403


if env("SYNTHETIC_TRIAL_ACK", default="") != "SYNTHETIC-DATA-ONLY":  # noqa: F405
    raise ImproperlyConfigured("This profile requires SYNTHETIC-DATA-ONLY acknowledgment.")

DEBUG = False
PRODUCTION_DEPLOYMENT = False
SYNTHETIC_TRIAL = True
OTP_PROVIDER = "synthetic_trial"
OTP_FIXED_CODE = env("OTP_FIXED_CODE")  # noqa: F405
if len(OTP_FIXED_CODE) != 6 or any(character not in "0123456789" for character in OTP_FIXED_CODE):
    raise ImproperlyConfigured("Synthetic trial OTP configuration is invalid.")

for _name in (
    "DJANGO_SECRET_KEY", "ACCOUNTS_CRYPTO_SECRET", "NOTIFICATIONS_CRYPTO_SECRET",
    "TOMBSTONE_HASH_KEY", "TOMBSTONE_SIGNING_KEY", "ANALYTICS_HASH_KEY", "AUDIT_HASH_KEY",
):
    if len(env(_name, default="")) < 32:  # noqa: F405
        raise ImproperlyConfigured("Synthetic trial requires independently configured secrets.")

APP_DOMAIN = env("APP_DOMAIN")  # noqa: F405
try:
    ipaddress.IPv4Address(APP_DOMAIN)
except ipaddress.AddressValueError:
    raise ImproperlyConfigured("Synthetic trial requires its explicit IPv4 endpoint.") from None
ALLOWED_HOSTS = [APP_DOMAIN, "127.0.0.1", "localhost"]
CSRF_TRUSTED_ORIGINS = [f"https://{APP_DOMAIN}"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 86400
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

DATABASES = {"default": env.db("DATABASE_URL")}  # noqa: F405
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("Synthetic cloud trial requires PostgreSQL.")
DATABASES["default"].update(CONN_MAX_AGE=60, CONN_HEALTH_CHECKS=True)
REDIS_URL = env("REDIS_URL")  # noqa: F405
CACHES = {"default": {
    "BACKEND": "django_redis.cache.RedisCache", "LOCATION": REDIS_URL,
    "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    "KEY_PREFIX": "emr-synthetic-trial", "TIMEOUT": 300,
}}
CELERY_BROKER_URL = env("CELERY_BROKER_URL")  # noqa: F405
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND")  # noqa: F405
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_ALWAYS_EAGER = False

DOCUMENT_STORAGE_BACKEND = "local"
DOCUMENT_STORAGE_ROOT = Path("/var/lib/phr/trial-objects")  # noqa: F405
EXPORT_TEMP_DIRECTORY = "/var/lib/phr/export-tmp"
WEBPUSH_ENABLED = False
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"

MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405
MIDDLEWARE.insert(2, "apps.core.trial.SyntheticTrialMiddleware")  # noqa: F405
TEMPLATES[0]["OPTIONS"]["context_processors"].append("apps.core.trial.trial_environment")  # noqa: F405
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MAX_AGE = 3600
WHITENOISE_ALLOW_ALL_ORIGINS = False
