import re


class InvalidPhone(ValueError):
    pass


def normalize_mainland_phone(raw):
    if not isinstance(raw, str):
        raise InvalidPhone("A valid mainland mobile number is required.")

    compact = re.sub(r"[\s-]", "", raw)
    if compact.startswith("+"):
        compact = compact[1:]
    if compact.startswith("86"):
        compact = compact[2:]

    if not re.fullmatch(r"1\d{10}", compact):
        raise InvalidPhone("A valid mainland mobile number is required.")
    return f"+86{compact}"
