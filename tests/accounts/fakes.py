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
