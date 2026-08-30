import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def clear_otp_cache():
    cache.clear()
