from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-secret-key"
ACCOUNTS_CRYPTO_SECRET = "test-accounts-crypto-secret"
ACCOUNTS_CRYPTO_SECRET_CONFIGURED = True
OTP_PROVIDER = "test"
OTP_FIXED_CODE = None
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
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
