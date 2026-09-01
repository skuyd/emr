import secrets

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password


def generate_code():
    configured = None
    if settings.DEBUG is True and getattr(settings, "OTP_PROVIDER", None) == "development":
        configured = getattr(settings, "OTP_FIXED_CODE", None)
    if configured is not None:
        if (
            not isinstance(configured, str)
            or len(configured) != 6
            or any(character not in "0123456789" for character in configured)
        ):
            raise ValueError("OTP fixed-code configuration is invalid.")
        return configured
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code):
    return make_password(code)


def code_matches(code, encoded):
    return isinstance(code, str) and len(code) == 6 and code.isdigit() and check_password(code, encoded)
