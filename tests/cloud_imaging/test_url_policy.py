import pytest
from django.core.exceptions import ValidationError


@pytest.mark.parametrize('value', [
    'javascript:alert(1)', 'file:///private', 'data:text/plain,private', '//example.invalid/view',
    'https://user:secret@example.invalid/', 'https://example.invalid\\@other.invalid/',
    'https://example.invalid/\nprivate', 'https://example.invalid/\u202eprivate',
    'http://127.0.0.1/', 'http://127.1/', 'http://[::1]/', 'http://192.168.1.4/',
    'http://localhost/', 'http://hospital.local/', 'https://singlehost/',
    'https://example.invalid:99999/', 'https://example.invalid/a b', '',
])
def test_unsafe_or_local_target_is_rejected_without_echoing_its_content(value):
    from apps.cloud_imaging.url_policy import validate_url

    with pytest.raises(ValidationError) as caught:
        validate_url(value)
    assert 'secret' not in str(caught.value) and 'example.invalid' not in str(caught.value)


def test_valid_url_preserves_access_parameters_and_shows_only_ascii_site():
    from apps.cloud_imaging.url_policy import validate_url

    raw = 'https://影像.example.invalid:8443/A%2fb?entry=first&entry=SECOND%2Bv#access-token'
    result = validate_url(raw)
    assert result.value == raw
    assert result.site_label == 'xn--o1qp07a.example.invalid:8443'
    assert 'access-token' not in repr(result)
