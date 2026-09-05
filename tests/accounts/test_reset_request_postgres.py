from concurrent.futures import ThreadPoolExecutor
import os

from django.db import close_old_connections, connection
from django.utils import timezone
import pytest

from apps.accounts.models import PasswordAttemptThrottle
from apps.accounts.sms_delivery import allow_password_reset_request


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


def test_concurrent_anonymous_reset_requests_share_one_durable_budget():
    if not os.environ.get("PHR_POSTGRES_TEST_URL"):
        pytest.skip("PHR_POSTGRES_TEST_URL is unavailable")
    assert connection.vendor == "postgresql"
    now = timezone.now()

    def request(_index):
        close_old_connections()
        try:
            return allow_password_reset_request("203.0.113.1", now=now)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(40)))
    assert sum(results) == 30
    assert PasswordAttemptThrottle.objects.get(scope="reset").attempts == 30
