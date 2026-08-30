from .base import *  # noqa: F403


DEBUG = True
SECRET_KEY = "container-static-build-only-secret"
ALLOWED_HOSTS = ["localhost"]
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
