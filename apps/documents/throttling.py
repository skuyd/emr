import hashlib
import hmac
import time

from django.conf import settings
from django.core.cache import cache

from apps.core.client_ip import get_client_ip


class UploadRateLimited(RuntimeError):
    code = "upload_rate_limited"


def _opaque_identifier(scope, value):
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        f"document-upload:{scope}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _increment(key, timeout):
    if cache.add(key, 1, timeout=timeout):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=timeout)
        return 1


def check_upload_rate(request, patient, *, now=None):
    now = time.time() if now is None else now
    window = int(now) // 60
    timeout = 65
    ip = get_client_ip(request)
    patient_key = f"phr-upload:patient:{_opaque_identifier('patient', patient.pk)}:{window}"
    ip_key = f"phr-upload:ip:{_opaque_identifier('ip', ip)}:{window}"
    patient_count = _increment(patient_key, timeout)
    ip_count = _increment(ip_key, timeout)
    if (
        patient_count > settings.DOCUMENT_UPLOAD_PATIENT_REQUESTS_PER_MINUTE
        or ip_count > settings.DOCUMENT_UPLOAD_IP_REQUESTS_PER_MINUTE
    ):
        raise UploadRateLimited()
