from typing import Protocol
from django.conf import settings


class SmsProvider(Protocol):
    def send_otp(self, phone, code): ...


class NullSmsProvider:
    def send_otp(self, phone, code):
        return None


class DevelopmentSmsProvider:
    def send_otp(self, phone, code):
        return None


def get_sms_provider():
    if settings.DEBUG and getattr(settings, "OTP_PROVIDER", None) == "console" and getattr(settings, "OTP_FIXED_CODE", None):
        return DevelopmentSmsProvider()
    class FailingProvider:
        def send_otp(self, phone, code):
            raise RuntimeError("OTP provider unavailable")
    return FailingProvider()
