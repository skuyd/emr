import re
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.urls import resolve

from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "shell-test"}


def _account_with_patient(django_user_model, seed, display_name="我自己"):
    account = django_user_model.objects.create(
        phone_hash=(seed * 64)[:64],
        phone_encrypted="ciphertext",
    )
    patient = create_patient_space(account, display_name, CONFIRMATIONS, EVIDENCE)
    return account, patient


@pytest.mark.django_db
def test_public_shell_uses_shared_brand_card_footer_and_no_business_navigation(client, django_user_model):
    anonymous = client.get("/login/")
    anonymous_content = anonymous.content.decode()

    assert anonymous.status_code == 200
    assert 'href="/static/css/components.css"' in anonymous_content
    assert "健康之家" in anonymous_content
    assert "家庭健康档案" in anonymous_content
    assert 'href="#main-content"' in anonymous_content
    assert 'class="public-main"' in anonymous_content
    assert 'href="/privacy/"' in anonymous_content
    assert 'href="/onboarding/sensitive-information/"' in anonymous_content
    assert 'class="mobile-nav"' not in anonymous_content
    assert "data-notification-center" not in anonymous_content
    assert 'href="/records/"' not in anonymous_content

    account, _patient = _account_with_patient(django_user_model, "p", "妈妈")
    client.force_login(account)
    authenticated_policy = client.get("/privacy/").content.decode()
    assert 'href="/me/"' in authenticated_policy
    assert "返回我的" in authenticated_policy

    source = Path("templates/base_public.html").read_text(encoding="utf-8")
    assert "{% block title %}健康之家｜家庭健康档案{% endblock %}" in source


@pytest.mark.django_db
def test_authenticated_shell_has_exact_primary_navigation_task_discovery_and_logout(client, django_user_model):
    account, patient = _account_with_patient(django_user_model, "i", "妈妈")
    client.force_login(account)

    response = client.get("/")
    content = response.content.decode()

    assert response.status_code == 200
    for required in ("<header", "<nav", "<main", 'href="#main-content"'):
        assert required in content
    for label in ("首页", "健康档案", "健康趋势", "我的"):
        assert f">{label}<" in content
    assert ">任务<" not in content
    assert 'href="/tasks/"' in content
    assert f"{patient.display_name}的健康档案" in content
    assert 'class="mobile-nav"' in content
    assert ">上传<" in content
    assert "<aside" not in content
    assert 'method="post" action="/logout/"' in content
    assert 'name="csrfmiddlewaretoken"' in content
    assert content.count('id="main-content"') == 1
    assert content.count('href="/uploads/new/"') >= 1
    assert "phone" not in content
    assert "diagnosis" not in content
    assert 'href="/static/css/components.css"' in content

    source = Path("templates/base_app.html").read_text(encoding="utf-8")
    assert "{% block title %}健康之家｜家庭健康档案{% endblock %}" in source


@pytest.mark.django_db
def test_application_routes_are_authenticated_and_not_dead(client, django_user_model):
    account, _patient = _account_with_patient(django_user_model, "j")
    client.force_login(account)

    records = client.get("/records/")
    assert records.status_code == 200
    assert "搜索资料" in records.content.decode()
    profile = client.get("/me/")
    assert profile.status_code == 200
    assert "当前试用配额" in profile.content.decode()
    assert client.get("/tasks/").status_code == 302
    assert client.get("/tasks/")["Location"] == "/#home-tasks-title"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "label"),
    [("/", "首页"), ("/records/", "健康档案"), ("/uploads/new/", "上传"), ("/me/", "我的")],
)
def test_shell_marks_exactly_one_current_destination_for_every_route(client, django_user_model, path, label):
    account, _patient = _account_with_patient(django_user_model, label)
    client.force_login(account)

    content = client.get(path).content.decode()

    assert content.count('aria-current="page"') == 1
    assert re.search(rf'<a[^>]*aria-current="page"[^>]*>{label}</a>', content)


def test_dynamic_indicator_route_marks_trends_instead_of_records(rf):
    request = rf.get("/trends/LAB_WBC/")
    request.resolver_match = resolve("/trends/LAB_WBC/")

    content = render_to_string(
        "components/_app_navigation.html",
        {
            "current_section": "records",
            "current_url_name": request.resolver_match.url_name,
        },
        request=request,
    )

    assert content.count('aria-current="page"') == 1
    assert re.search(r'<a[^>]*aria-current="page"[^>]*>健康趋势</a>', content)


def test_app_shell_styles_keep_fixed_navigation_focus_and_responsive_overflow_contract():
    css = Path("static/css/app-shell.css").read_text(encoding="utf-8")
    notifications_css = Path("static/css/notifications.css").read_text(encoding="utf-8")
    javascript = Path("static/js/app-shell.js").read_text(encoding="utf-8")

    assert ":root" not in css
    assert ".app-header" in css and "position: sticky" in css
    assert re.search(r"\.app-header\s*\{[^}]*height:\s*7[2-9]px", css, re.DOTALL)
    assert ".mobile-nav" in css
    assert "position: fixed" in css
    assert "env(safe-area-inset-bottom" in css
    assert "height: 66px" in css
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    assert "min-height: 44px" in css
    assert "max-width: 100%" in css
    assert "overflow-x" in css
    assert ":focus-visible" in css
    assert "@media (forced-colors: active)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "var(--color-paper)" in css
    assert "var(--line)" in css
    assert ":root" not in notifications_css
    assert "app-nav-toggle" not in javascript
    assert "data-shell" not in javascript
    assert ".app-sidebar" not in css
    assert not re.search(r"width:\s*1?2?8?0px", css)
