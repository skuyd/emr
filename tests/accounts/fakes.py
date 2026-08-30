class RecordingSmsProvider:
    def __init__(self):
        self.codes = []

    @property
    def last_code(self):
        return self.codes[-1]

    def send_otp(self, phone, code):
        self.codes.append(code)


class FailingSmsProvider:
    def send_otp(self, phone, code):
        raise RuntimeError("delivery unavailable")


class LeakySmsProvider:
    def send_otp(self, phone, code):
        raise RuntimeError(f"delivery failed for {phone} with code {code}")


class SimulatedProcessDeath(BaseException):
    pass


class CrashingAfterAcceptingSmsProvider(RecordingSmsProvider):
    def send_otp(self, phone, code):
        super().send_otp(phone, code)
        raise SimulatedProcessDeath()
