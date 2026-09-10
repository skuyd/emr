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


def test_site_display_matches_modern_browser_idna_without_changing_the_host():
    from apps.cloud_imaging.url_policy import validate_url
    result = validate_url('https://faß.example.invalid/view?key=unchanged')
    assert result.value == 'https://faß.example.invalid/view?key=unchanged'
    assert result.site_label == 'xn--fa-hia.example.invalid'


@pytest.mark.parametrize('value', [
    'http://10.0.0.1/', 'http://169.254.169.254/', 'http://[fc00::1]/',
    'http://[fe80::1]/', 'http://0x7f000001/', 'http://2130706433/',
    'http://127.0.0.1./', 'http://0177.0.0.1/', 'https://host.internal/',
    'https://host.lan/', 'https://host.home/', 'https://example.invalid:0/',
    'https://example.invalid:/', 'https://example.invalid/%',
])
def test_additional_local_and_incomplete_targets_cannot_be_confirmed(value):
    from apps.cloud_imaging.url_policy import validate_url
    with pytest.raises(ValidationError):
        validate_url(value)


@pytest.mark.parametrize('raw,site', [
    ('HTTPS://EXAMPLE.INVALID:443/a%2fb?next=https://other.invalid/a&x=1&x=2#access', 'example.invalid'),
    ('http://example.invalid:8080/a?q=%2B%25#token', 'example.invalid:8080'),
    ('https://8.8.8.8/view', '8.8.8.8'),
    ('https://[2606:4700:4700::1111]/view', '[2606:4700:4700::1111]'),
])
def test_url_validation_never_resolves_or_requests_a_host_and_retains_parameter_semantics(raw, site, monkeypatch):
    import socket
    import urllib.request
    from apps.cloud_imaging.url_policy import validate_url
    def forbidden(*args, **kwargs):
        pytest.fail('Local validation must never resolve or fetch a target')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    result = validate_url(raw)
    assert result.value == raw and result.site_label == site
