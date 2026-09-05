from types import SimpleNamespace

from django.core.cache import cache
from django.test import RequestFactory

from apps.documents.throttling import check_upload_rate


def test_upload_limit_keeps_distinct_trusted_proxy_clients_separate(settings):
    settings.TRUSTED_PROXY_NETWORKS = ["172.30.50.2/32"]
    settings.DOCUMENT_UPLOAD_IP_REQUESTS_PER_MINUTE = 1
    cache.clear()
    factory = RequestFactory()
    try:
        for index, ip in enumerate(("192.0.2.1", "192.0.2.2")):
            request = factory.post("/", REMOTE_ADDR="172.30.50.2", HTTP_X_FORWARDED_FOR=ip)
            check_upload_rate(request, SimpleNamespace(pk=index), now=120)
    finally:
        cache.clear()
