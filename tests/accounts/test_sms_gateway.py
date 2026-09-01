import hashlib
import hmac
import json

import pytest

from apps.accounts.providers import HttpsSmsGatewayProvider, SmsGatewayUnavailable, get_sms_provider


class Response:
    def __init__(self, status=202, payload=b'{"accepted":true}'):
        self.status = status
        self.payload = payload

    def read(self, amount):
        return self.payload[:amount]


class Connection:
    def __init__(self, host, port, timeout, *, response=None):
        self.init = (host, port, timeout)
        self.response = response or Response()
        self.request_value = None
        self.closed = False

    def request(self, method, path, body, headers):
        self.request_value = (method, path, body, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def provider(connection):
    return HttpsSmsGatewayProvider(
        "https://sms-gateway.example.test/v1/send",
        "api-key",
        "signing-secret",
        "login-template",
        ["sms-gateway.example.test"],
        timeout=7,
        connection_factory=lambda host, port, timeout: connection,
        clock=lambda: 1_800_000_000,
        nonce_factory=lambda: "fixed-nonce-123456",
    )


def test_https_gateway_sends_exact_bounded_signed_contract_and_closes_connection():
    connection = Connection("ignored", 443, 7)

    provider(connection).send_otp("13800138000", "123456", "password_reset")

    method, path, body, headers = connection.request_value
    assert method == "POST" and path == "/v1/send"
    assert json.loads(body) == {
        "code": "123456",
        "phone": "13800138000",
        "purpose": "password_reset",
        "template_id": "login-template",
    }
    signed = b"1800000000\nfixed-nonce-123456\n" + body
    expected = hmac.new(b"signing-secret", signed, hashlib.sha256).hexdigest()
    assert headers == {
        "Authorization": "Bearer api-key",
        "Content-Type": "application/json",
        "X-PHR-Nonce": "fixed-nonce-123456",
        "X-PHR-Signature": f"v1={expected}",
        "X-PHR-Timestamp": "1800000000",
    }
    assert connection.closed is True
    assert "api-key" not in repr(provider(connection))


@pytest.mark.parametrize(
    "url,allowed_hosts",
    [
        ("http://sms.example.test/send", ["sms.example.test"]),
        ("https://127.0.0.1/send", ["127.0.0.1"]),
        ("https://metadata.internal/send", ["sms.example.test"]),
        ("https://user:pass@sms.example.test/send", ["sms.example.test"]),
        ("https://sms.example.test:8443/send", ["sms.example.test"]),
        ("https://sms.example.test/send?redirect=true", ["sms.example.test"]),
        ("https://[invalid/send", ["invalid"]),
        ("https://sms.example.test:invalid/send", ["sms.example.test"]),
        ("https://sms.example.test/send", None),
    ],
)
def test_gateway_configuration_rejects_non_tls_ssrf_and_credential_targets(url, allowed_hosts):
    with pytest.raises(SmsGatewayUnavailable):
        HttpsSmsGatewayProvider(url, "key", "secret", "template", allowed_hosts)


@pytest.mark.parametrize(
    "response",
    [
        Response(status=302),
        Response(status=500),
        Response(payload=b"x" * 4097),
        Response(payload=b"not-json"),
        Response(payload=b'{"accepted":false,"detail":"private-provider-detail"}'),
    ],
)
def test_gateway_fails_closed_without_echoing_provider_payload(response):
    connection = Connection("ignored", 443, 7, response=response)

    with pytest.raises(SmsGatewayUnavailable) as raised:
        provider(connection).send_otp("13800138000", "123456", "sign_in")

    assert "private-provider-detail" not in str(raised.value)
    assert connection.closed is True


def test_provider_selector_builds_gateway_only_from_explicit_settings(settings):
    settings.DEBUG = False
    settings.OTP_PROVIDER = "https_gateway"
    settings.SMS_GATEWAY_URL = "https://sms.example.test/send"
    settings.SMS_GATEWAY_API_KEY = "key"
    settings.SMS_GATEWAY_SIGNING_SECRET = "secret"
    settings.SMS_GATEWAY_TEMPLATE_ID = "template"
    settings.SMS_GATEWAY_ALLOWED_HOSTS = ["sms.example.test"]
    settings.SMS_GATEWAY_TIMEOUT_SECONDS = 5

    selected = get_sms_provider()

    assert isinstance(selected, HttpsSmsGatewayProvider)


@pytest.mark.parametrize("field", ["api_key", "signing_secret", "template_id"])
def test_gateway_rejects_control_characters_in_credentials_and_template(field):
    values = {
        "api_key": "key",
        "signing_secret": "secret",
        "template_id": "template",
    }
    values[field] += "\nunsafe"

    with pytest.raises(SmsGatewayUnavailable):
        HttpsSmsGatewayProvider(
            "https://sms.example.test/send",
            values["api_key"],
            values["signing_secret"],
            values["template_id"],
            ["sms.example.test"],
        )


def test_gateway_rejects_invalid_nonce_before_network_delivery():
    connection = Connection("ignored", 443, 7)
    selected = HttpsSmsGatewayProvider(
        "https://sms.example.test/send",
        "key",
        "secret",
        "template",
        ["sms.example.test"],
        connection_factory=lambda host, port, timeout: connection,
        nonce_factory=lambda: "unsafe\nnonce",
    )

    with pytest.raises(SmsGatewayUnavailable):
        selected.send_otp("13800138000", "123456", "sign_in")
    assert connection.request_value is None
