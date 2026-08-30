import logging

import pytest
from django.test import Client, override_settings

from apps.accounts.services import DeliveryFailed, InvalidOtp, ThrottledOtp
from apps.accounts.providers import get_sms_provider
from tests.accounts.fakes import RecordingSmsProvider


@pytest.mark.django_db
def test_login_page_has_one_accessible_shared_form_and_real_privacy_link(client):
    response = client.get("/login/")

    assert response.status_code == 200
    content = response.content.decode()
    for text in ["手机号", "验证码", "获取验证码", "登录", "隐私政策"]:
        assert text in content
    assert content.count('id="id_phone"') == 1
    assert content.count('id="id_code"') == 1
    assert 'formaction="/login/request-code/"' in content
    assert 'formaction="/login/verify/"' in content
    assert 'href="/privacy/"' in content
    assert 'href="#main-content"' in content
    assert '<main id="main-content"' in content
    assert client.get("/privacy/").status_code == 200


def test_login_actions_are_post_only(client):
    assert client.get("/login/request-code/").status_code == 405
    assert client.get("/login/verify/").status_code == 405
    assert client.get("/logout/").status_code == 405


@override_settings(DEBUG=False, OTP_PROVIDER="console", OTP_FIXED_CODE="123456")
def test_provider_selector_fails_closed_outside_development():
    with pytest.raises(RuntimeError):
        get_sms_provider().send_otp("ignored", "000000")


@pytest.mark.django_db
def test_request_code_requires_csrf(client):
    csrf_client = Client(enforce_csrf_checks=True)
    response = csrf_client.post("/login/request-code/", {"phone": "13800138000"})
    assert response.status_code == 403
    assert csrf_client.post("/login/verify/", {"phone": "13800138000", "code": "123456"}).status_code == 403
    assert csrf_client.post("/logout/").status_code == 403


@pytest.mark.django_db
def test_invalid_phone_uses_exact_generic_error_and_never_echoes_input(client):
    response = client.post("/login/request-code/", {"phone": "not-a-phone"})
    content = response.content.decode()
    assert "请输入正确的手机号" in content
    assert "not-a-phone" not in content
    assert response.context["request_accepted"] is False


@pytest.mark.django_db
def test_throttle_and_delivery_failures_have_distinct_safe_messages(client, monkeypatch):
    monkeypatch.setattr("apps.accounts.views.request_otp", lambda *args: (_ for _ in ()).throw(ThrottledOtp()))
    response = client.post("/login/request-code/", {"phone": "13800138000"})
    assert "操作过于频繁，请稍后再试" in response.content.decode()

    monkeypatch.setattr("apps.accounts.views.request_otp", lambda *args: (_ for _ in ()).throw(DeliveryFailed()))
    response = client.post("/login/request-code/", {"phone": "13800138000"})
    assert "暂时无法登录，请检查网络后重试" in response.content.decode()


@pytest.mark.django_db
@override_settings(OTP_FIXED_CODE="123456")
def test_accepted_request_starts_countdown_and_fixed_code_login_sets_epoch_timestamps(client, monkeypatch):
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)

    response = client.post("/login/request-code/", {"phone": "13800138000", "next": "/after/?page=2"})
    assert response.context["request_accepted"] is True
    assert "data-request-accepted" in response.content.decode()
    assert provider.last_code == "123456"

    response = client.post(
        "/login/verify/?next=/after/?page=2",
        {"phone": "13800138000", "code": provider.last_code, "next": "/after/?page=2"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/onboarding/"
    assert client.session["post_onboarding_next"] == "/after/?page=2"
    assert isinstance(client.session["session_started_at"], int)
    assert isinstance(client.session["session_last_seen_at"], int)


@pytest.mark.django_db
def test_invalid_or_expired_code_is_not_distinguished_or_echoed(client, monkeypatch):
    monkeypatch.setattr("apps.accounts.views.verify_otp", lambda *args: (_ for _ in ()).throw(InvalidOtp()))
    response = client.post("/login/verify/", {"phone": "13800138000", "code": "654321"})
    content = response.content.decode()
    assert "验证码无效，请重新获取" in content
    assert "654321" not in content


@pytest.mark.parametrize(
    "next_value",
    ["//evil.example", "https://evil.example/", "/\\evil", "/%5Cevil", "/%255Cevil", "/%0devil", "/%250devil", "javascript:alert(1)", "/login/", "/login/%252e%252e/login/", "/logout/"],
)
@pytest.mark.django_db
def test_unsafe_next_variants_are_not_preserved_or_followed(client, monkeypatch, next_value):
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    response = client.get("/login/", {"next": next_value})
    assert response.context["form"].initial["next"] == ""

    client.post("/login/request-code/", {"phone": "13800138000", "next": next_value})
    response = client.post(
        "/login/verify/",
        {"phone": "13800138000", "code": provider.last_code, "next": next_value},
    )
    assert response["Location"] == "/onboarding/"
    assert "post_onboarding_next" not in client.session


@pytest.mark.django_db
def test_valid_phone_is_preserved_but_code_is_cleared_after_request_and_errors(client, monkeypatch):
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    response = client.post("/login/request-code/", {"phone": "13800138000"})
    assert 'value="13800138000"' in response.content.decode()
    assert 'value="' not in response.content.decode().split('id="id_code"', 1)[1].split(">", 1)[0]

    monkeypatch.setattr("apps.accounts.views.verify_otp", lambda *args: (_ for _ in ()).throw(InvalidOtp()))
    response = client.post("/login/verify/", {"phone": "13800138000", "code": "654321"})
    content = response.content.decode()
    assert 'value="13800138000"' in content
    assert "654321" not in content


@pytest.mark.django_db
def test_authenticated_login_page_redirects_to_safe_next(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="a" * 64, phone_encrypted="ciphertext")
    from apps.patients.services import create_patient_space

    create_patient_space(
        account,
        "\u738b\u5c0f\u660e",
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "test"},
    )
    client.force_login(account)
    assert client.get("/login/?next=/continue/?q=1")["Location"] == "/continue/?q=1"


@pytest.mark.django_db
def test_login_messages_and_logger_records_do_not_include_raw_phone_or_code(client, monkeypatch, caplog):
    provider = RecordingSmsProvider()
    monkeypatch.setattr("apps.accounts.views.get_sms_provider", lambda: provider)
    with caplog.at_level(logging.INFO):
        response = client.post("/login/request-code/", {"phone": "13800138000"})
    content = response.content.decode()
    assert 'value="13800138000"' in content
    assert "13800138000" not in "验证码已发送，请在五分钟内完成登录。"
    assert "13800138000" not in "\n".join(record.getMessage() for record in caplog.records)
    assert provider.last_code not in content
