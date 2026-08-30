from typing import Protocol


class SmsProvider(Protocol):
    def send_otp(self, phone, code): ...


class NullSmsProvider:
    def send_otp(self, phone, code):
        return None
