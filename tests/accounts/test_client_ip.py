import pytest
from django.test import RequestFactory, override_settings


@pytest.mark.parametrize("peer,forwarded,trusted,expected", [
    ("198.51.100.10", "203.0.113.25", [], "198.51.100.10"),
    ("172.20.0.10", "203.0.113.25", ["172.20.0.10/32"], "203.0.113.25"),
    ("172.20.0.10", "192.0.2.1, 198.51.100.10", ["172.20.0.10/32"], "198.51.100.10"),
    ("172.20.0.10", "203.0.113.25, 172.20.0.11", ["172.20.0.0/24"], "203.0.113.25"),
    ("172.20.0.10", "garbage, 203.0.113.25", ["172.20.0.0/24"], "203.0.113.25"),
    ("172.20.0.10", "203.0.113.25, garbage", ["172.20.0.0/24"], "172.20.0.10"),
    ("2001:db8:1::1", "2001:db8:2::0025", ["2001:db8:1::/64"], "2001:db8:2::25"),
    ("172.20.0.10", "", ["172.20.0.0/24"], "172.20.0.10"),
    ("172.20.0.10", ",".join(["203.0.113.25"] * 33), ["172.20.0.0/24"], "172.20.0.10"),
    ("172.20.0.10", "fe80::25%eth0", ["172.20.0.0/24"], "172.20.0.10"),
    ("not-an-address", "203.0.113.25", ["172.20.0.0/24"], ""),
])
def test_client_ip_only_strips_trusted_proxy_hops(peer, forwarded, trusted, expected):
    from apps.core.client_ip import get_client_ip

    request = RequestFactory().get("/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR=forwarded)
    with override_settings(TRUSTED_PROXY_NETWORKS=trusted):
        assert get_client_ip(request) == expected


@pytest.mark.django_db
@override_settings(TRUSTED_PROXY_NETWORKS=["172.20.0.10/32"])
def test_proxy_clients_have_independent_password_limits(client, django_user_model, monkeypatch):
    from apps.accounts.crypto import hash_phone
    from tests.accounts.fakes import RecordingSmsProvider

    django_user_model.objects.create_user(
        phone_hash=hash_phone("+8613800138000"), phone_encrypted="synthetic", password="Correct synthetic passphrase"
    )
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    for _ in range(30):
        client.post("/login/password/", {"phone": "13700137000", "password": "wrong"},
                    REMOTE_ADDR="172.20.0.10", HTTP_X_FORWARDED_FOR="198.51.100.10")
    response = client.post("/login/password/", {"phone": "13800138000", "password": "Correct synthetic passphrase"},
                           REMOTE_ADDR="172.20.0.10", HTTP_X_FORWARDED_FOR="203.0.113.25")
    assert response.status_code == 302
    assert len(provider.codes) == 1


@override_settings(TRUSTED_PROXY_NETWORKS=["172.20.0.10/32"])
def test_consent_request_evidence_uses_the_same_client_boundary():
    from apps.patients.views import _request_evidence

    request = RequestFactory().post("/onboarding/", REMOTE_ADDR="172.20.0.10", HTTP_X_FORWARDED_FOR="203.0.113.25")
    assert _request_evidence(request)["ip"] == "203.0.113.25"
