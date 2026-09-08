import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="unsafe-development-key-change-before-deployment")
ACCOUNTS_CRYPTO_SECRET = env("ACCOUNTS_CRYPTO_SECRET", default=SECRET_KEY)
ACCOUNTS_CRYPTO_SECRET_CONFIGURED = "ACCOUNTS_CRYPTO_SECRET" in os.environ
ANALYTICS_HASH_KEY = env("ANALYTICS_HASH_KEY", default=SECRET_KEY)
ANALYTICS_HASH_KEY_CONFIGURED = "ANALYTICS_HASH_KEY" in os.environ
AUDIT_HASH_KEY = env("AUDIT_HASH_KEY", default=SECRET_KEY)
AUDIT_HASH_KEY_CONFIGURED = "AUDIT_HASH_KEY" in os.environ
TOMBSTONE_HASH_KEY = env("TOMBSTONE_HASH_KEY", default=SECRET_KEY)
TOMBSTONE_HASH_KEY_CONFIGURED = "TOMBSTONE_HASH_KEY" in os.environ
TOMBSTONE_SIGNING_KEY = env("TOMBSTONE_SIGNING_KEY", default=SECRET_KEY)
TOMBSTONE_SIGNING_KEY_CONFIGURED = "TOMBSTONE_SIGNING_KEY" in os.environ
NOTIFICATIONS_CRYPTO_SECRET = env("NOTIFICATIONS_CRYPTO_SECRET", default=ACCOUNTS_CRYPTO_SECRET)
NOTIFICATIONS_CRYPTO_SECRET_CONFIGURED = "NOTIFICATIONS_CRYPTO_SECRET" in os.environ
WEBPUSH_ENABLED = env.bool("WEBPUSH_ENABLED", default=False)
WEBPUSH_VAPID_PUBLIC_KEY = env("WEBPUSH_VAPID_PUBLIC_KEY", default="")
WEBPUSH_VAPID_PRIVATE_KEY = env("WEBPUSH_VAPID_PRIVATE_KEY", default="")
WEBPUSH_VAPID_SUBJECT = env("WEBPUSH_VAPID_SUBJECT", default="")
WEBPUSH_TTL_SECONDS = env.int("WEBPUSH_TTL_SECONDS", default=300)
WEBPUSH_TIMEOUT_SECONDS = env.int("WEBPUSH_TIMEOUT_SECONDS", default=10)
WEBPUSH_ALLOWED_ENDPOINT_HOSTS = env.list(
    "WEBPUSH_ALLOWED_ENDPOINT_HOSTS",
    default=[
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
        ".notify.windows.com",
    ],
)
SMS_GATEWAY_URL = env("SMS_GATEWAY_URL", default="")
SMS_GATEWAY_API_KEY = env("SMS_GATEWAY_API_KEY", default="")
SMS_GATEWAY_SIGNING_SECRET = env("SMS_GATEWAY_SIGNING_SECRET", default="")
SMS_GATEWAY_TEMPLATE_ID = env("SMS_GATEWAY_TEMPLATE_ID", default="")
SMS_GATEWAY_ALLOWED_HOSTS = env.list("SMS_GATEWAY_ALLOWED_HOSTS", default=[])
SMS_GATEWAY_TIMEOUT_SECONDS = env.int("SMS_GATEWAY_TIMEOUT_SECONDS", default=10)
OPERATIONS_METRICS_TOKEN = env("OPERATIONS_METRICS_TOKEN", default="")
OPERATIONS_METRICS_TOKEN_CONFIGURED = "OPERATIONS_METRICS_TOKEN" in os.environ
OPERATIONS_READINESS_TIMEOUT_SECONDS = max(0.1, min(3.0, env.float("OPERATIONS_READINESS_TIMEOUT_SECONDS", default=1.0)))
OPERATIONS_READINESS_CACHE_SECONDS = max(0.0, min(5.0, env.float("OPERATIONS_READINESS_CACHE_SECONDS", default=2.0)))
OPERATIONS_ALERT_QUEUE_THRESHOLD = env.int("OPERATIONS_ALERT_QUEUE_THRESHOLD", default=100)
OPERATIONS_ALERT_DELETION_THRESHOLD = env.int("OPERATIONS_ALERT_DELETION_THRESHOLD", default=20)
OPERATIONS_ALERT_PROVIDER_ERROR_THRESHOLD = env.int("OPERATIONS_ALERT_PROVIDER_ERROR_THRESHOLD", default=10)
DEBUG = env.bool("DJANGO_DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
TRUSTED_PROXY_NETWORKS = env.list("TRUSTED_PROXY_NETWORKS", default=[])
PRODUCTION_DEPLOYMENT = False
ALLOW_PERFORMANCE_SEED = env.bool("ALLOW_PERFORMANCE_SEED", default=False)
RESTORE_DRILL_MODE = env.bool("RESTORE_DRILL_MODE", default=False)

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
    "apps.labs.apps.LabsConfig",
    "apps.facts.apps.FactsConfig",
    "apps.lesions.apps.LesionsConfig",
    "apps.self_records.apps.SelfRecordsConfig",
    "apps.exports.apps.ExportsConfig",
    "apps.notifications.apps.NotificationsConfig",
    "apps.analytics.apps.AnalyticsConfig",
    "apps.operations.apps.OperationsConfig",
    "apps.core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.patients.link_privacy.FamilyLinkPrivacyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.operations.patient_audit.PatientAuditMiddleware",
    "apps.accounts.session.SessionExpiryMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.security.SecurityHeadersMiddleware",
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
                "apps.documents.context_processors.task_navigation",
                "apps.notifications.context_processors.notification_center",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "accounts.Account"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/login/"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=not DEBUG)
SESSION_COOKIE_AGE = 7 * 24 * 60 * 60
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=not DEBUG)
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=not DEBUG)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if env.bool("DJANGO_TRUST_X_FORWARDED_PROTO", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "sensitive_data": {"()": "apps.core.logging.SensitiveDataFilter"},
    },
    "formatters": {
        "safe": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "safe_console": {
            "class": "logging.StreamHandler",
            "filters": ["sensitive_data"],
            "formatter": "safe",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["safe_console"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {"handlers": ["safe_console"], "level": "INFO"},
}

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
DOCUMENT_S3_ALLOWED_HOSTS = env.list("DOCUMENT_S3_ALLOWED_HOSTS", default=[])
DOCUMENT_S3_ALLOW_INSECURE_INTERNAL = env.bool("DOCUMENT_S3_ALLOW_INSECURE_INTERNAL", default=False)
DOCUMENT_UPLOAD_PATIENT_REQUESTS_PER_MINUTE = env.int("DOCUMENT_UPLOAD_PATIENT_REQUESTS_PER_MINUTE", default=120)
DOCUMENT_UPLOAD_IP_REQUESTS_PER_MINUTE = env.int("DOCUMENT_UPLOAD_IP_REQUESTS_PER_MINUTE", default=300)
PROCESSING_PIPELINE_FACTORY = env(
    "PROCESSING_PIPELINE_FACTORY",
    default="apps.processing.pipeline.build_default_pipeline",
)
PROCESSING_DISPATCH_ON_UPLOAD = env.bool("PROCESSING_DISPATCH_ON_UPLOAD", default=True)
PHR_OCR_PROVIDER = env("PHR_OCR_PROVIDER", default="paddle")
PHR_OCR_DEVICE = env("PHR_OCR_DEVICE", default="cpu")
PHR_OCR_PADDLE_DETECTION_MODEL = env("PHR_OCR_PADDLE_DETECTION_MODEL", default="PP-OCRv5_mobile_det")
PHR_OCR_PADDLE_RECOGNITION_MODEL = env("PHR_OCR_PADDLE_RECOGNITION_MODEL", default="PP-OCRv5_mobile_rec")
PHR_OCR_PADDLE_DETECTION_MODEL_DIR = env("PHR_OCR_PADDLE_DETECTION_MODEL_DIR", default="")
PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR = env("PHR_OCR_PADDLE_RECOGNITION_MODEL_DIR", default="")
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
EXPORT_TEMP_DIRECTORY = env("EXPORT_TEMP_DIRECTORY", default="")

CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "control"
CELERY_TASK_ROUTES = {"processing.process_document": {"queue": "ocr"}}
CELERY_BEAT_SCHEDULE = {
    "expire-patient-shares": {
        "task": "patients.expire_shares",
        "schedule": 60.0,
    },
    "recover-export-jobs": {
        "task": "exports.recover_jobs",
        "schedule": 60.0,
    },
    "recover-sms-deliveries": {
        "task": "accounts.recover_sms_deliveries",
        "schedule": 5.0,
    },
    "recover-stale-processing-runs": {
        "task": "processing.recover_stale_runs",
        "schedule": 60.0,
    },
    "recover-document-deletion-jobs": {
        "task": "documents.recover_deletion_jobs",
        "schedule": 60.0,
    },
    "recover-account-deletion-jobs": {
        "task": "accounts.recover_deletion_jobs",
        "schedule": 60.0,
    },
    "recover-push-deliveries": {
        "task": "notifications.recover_push_deliveries",
        "schedule": 60.0,
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
