import os

from django.core.exceptions import ImproperlyConfigured
import environ

from .test import *  # noqa: F403


POSTGRES_TEST_URL = os.environ.get("PHR_POSTGRES_TEST_URL")
if not POSTGRES_TEST_URL:
    raise ImproperlyConfigured("PHR_POSTGRES_TEST_URL is required for PostgreSQL integration tests")

DATABASES = {"default": environ.Env.db_url_config(POSTGRES_TEST_URL)}
