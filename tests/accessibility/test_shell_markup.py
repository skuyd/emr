import re

import pytest

from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "shell-test"}


@pytest.mark.django_db
def test_application_shell_has_landmarks_navigation_logout_and_disabled_upload(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="i" * 64, phone_encrypted="ciphertext")
    patient = create_patient_space(account, "妈妈", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)

    response = client.get("/")
    content = response.content.decode()

    assert response.status_code == 200
    for required in ("<header", "<aside", "<nav", "<main", 'href="#main-content"', "首页", "病案", "我的", "任务状态", patient.display_name):
        assert required in content
    assert 'method="post" action="/logout/"' in content
    assert 'name="csrfmiddlewaretoken"' in content
    assert content.count('id="main-content"') == 1
    assert content.count("上传资料") == 1
    assert re.search(r"<button[^>]*disabled[^>]*>上传资料</button>", content)
    assert "资料上传功能即将开放" in content
    assert "phone" not in content
    assert "diagnosis" not in content


@pytest.mark.django_db
def test_application_placeholder_routes_are_authenticated_and_not_dead(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="j" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "我自己", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)

    for path in ("/records/", "/me/", "/tasks/"):
        response = client.get(path)
        assert response.status_code == 200
        assert "暂未开放" in response.content.decode()


def test_app_shell_styles_keep_fixed_navigation_focus_and_responsive_overflow_contract():
    css = open("static/css/app-shell.css", encoding="utf-8").read()
    javascript = open("static/js/app-shell.js", encoding="utf-8").read()

    assert ".app-sidebar" in css and "position: fixed" in css
    assert "minmax(0," in css
    assert "overflow-x: hidden" in css
    assert ":focus-visible" in css
    assert "@media" in css
    assert not re.search(r"width:\s*1?2?8?0px", css)
    assert "app-nav-toggle" in javascript
