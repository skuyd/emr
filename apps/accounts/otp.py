import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password


def generate_code():
    configured = getattr(settings, "OTP_FIXED_CODE", None)
    if configured is not None:
        if not isinstance(configured, str) or not configured.isdigit() or len(configured) != 6:
            raise ValueError("OTP fixed code must contain six digits.")
        return configured
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code):
    return make_password(code)


def code_matches(code, encoded):
    return isinstance(code, str) and len(code) == 6 and code.isdigit() and check_password(code, encoded)
