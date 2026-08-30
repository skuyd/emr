import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_login_page_contains_required_controls(client):
    response = client.get("/login/")

    assert response.status_code == 200
    content = response.content.decode()
    for text in ["手机号", "验证码", "获取验证码", "登录", "隐私政策"]:
        assert text in content
    assert 'for="id_phone"' in content
    assert 'for="id_code"' in content


def test_login_actions_are_post_only(client):
    assert client.get("/login/request-code/").status_code == 405
    assert client.get("/login/verify/").status_code == 405
    assert client.get("/logout/").status_code == 405


@pytest.mark.django_db
def test_request_code_requires_csrf_and_never_echoes_phone(client):
    csrf_client = client.__class__(enforce_csrf_checks=True)
    response = csrf_client.post("/login/request-code/", {"phone": "13800138000"})

    assert response.status_code == 403
    response = client.post("/login/request-code/", {"phone": "not-a-phone"})
    assert response.status_code == 200
    assert "not-a-phone" not in response.content.decode()


@pytest.mark.django_db
def test_unsafe_next_is_not_preserved(client):
    response = client.get("/login/?next=//evil.example")

    assert response.status_code == 200
    assert "evil.example" not in response.content.decode()
