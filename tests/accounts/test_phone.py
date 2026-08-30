import pytest

from apps.accounts.phone import InvalidPhone, normalize_mainland_phone


@pytest.mark.parametrize(
    "raw",
    ["13800138000", "+86 138 0013 8000", "86-13800138000"],
)
def test_normalizes_mainland_mobile(raw):
    assert normalize_mainland_phone(raw) == "+8613800138000"


@pytest.mark.parametrize(
    "raw",
    ["", "123", "+85291234567", "1380013800a", "+8610000000000", "+8612000000000"],
)
def test_rejects_non_mainland_or_malformed_phone(raw):
    with pytest.raises(InvalidPhone):
        normalize_mainland_phone(raw)
