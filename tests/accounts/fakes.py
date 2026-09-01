class RecordingSmsProvider:
    def __init__(self):
        self.codes = []
        self.purposes = []

    @property
    def last_code(self):
        return self.codes[-1]

    @property
    def last_purpose(self):
        return self.purposes[-1]

    def send_otp(self, phone, code, purpose):
        self.codes.append(code)
        self.purposes.append(purpose)


class FailingSmsProvider:
    def send_otp(self, phone, code, purpose):
        raise RuntimeError("delivery unavailable")


class LeakySmsProvider:
    def send_otp(self, phone, code, purpose):
        raise RuntimeError(f"delivery failed for {phone} with code {code}")


class SimulatedProcessDeath(BaseException):
    pass


class CrashingAfterAcceptingSmsProvider(RecordingSmsProvider):
    def send_otp(self, phone, code, purpose):
        super().send_otp(phone, code, purpose)
        raise SimulatedProcessDeath()
