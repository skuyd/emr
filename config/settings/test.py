from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-secret-key"
ACCOUNTS_CRYPTO_SECRET = "test-accounts-crypto-secret"
ACCOUNTS_CRYPTO_SECRET_CONFIGURED = True
NOTIFICATIONS_CRYPTO_SECRET = "test-notifications-crypto-secret"
NOTIFICATIONS_CRYPTO_SECRET_CONFIGURED = True
WEBPUSH_ALLOWED_ENDPOINT_HOSTS = ["push.example.test"]
TOMBSTONE_HASH_KEY = "test-tombstone-hash-key"
TOMBSTONE_HASH_KEY_CONFIGURED = True
TOMBSTONE_SIGNING_KEY = "test-tombstone-signing-key"
TOMBSTONE_SIGNING_KEY_CONFIGURED = True
ANALYTICS_HASH_KEY = "test-analytics-hash-key"
ANALYTICS_HASH_KEY_CONFIGURED = True
AUDIT_HASH_KEY = "test-audit-hash-key"
AUDIT_HASH_KEY_CONFIGURED = True
OPERATIONS_METRICS_TOKEN = "test-operations-metrics-token-32-bytes"
OPERATIONS_METRICS_TOKEN_CONFIGURED = True
OTP_PROVIDER = "test"
OTP_FIXED_CODE = None
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = False
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-cache",
    },
}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
PROCESSING_DISPATCH_ON_UPLOAD = False
