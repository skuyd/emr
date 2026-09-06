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


def _navigation(content, class_name):
    match = re.search(rf'<nav class="{class_name}"[^>]*>(.*?)</nav>', content, re.DOTALL)
    assert match is not None
    return match.group(1)


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
    assert 'class="desktop-nav"' not in anonymous_content
    assert "data-notification-center" not in anonymous_content
    assert 'href="/records/"' not in anonymous_content

    account, _patient = _account_with_patient(django_user_model, "p", "妈妈")
    client.force_login(account)
    authenticated_policy = client.get("/privacy/").content.decode()
    assert 'href="/me/"' in authenticated_policy
    assert "返回我的" in authenticated_policy

    source = Path("templates/base_public.html").read_text(encoding="utf-8")
    assert "{% block title %}健康之家｜家庭健康档案{% endblock %}" in source


@pytest.mark.parametrize(
    ("path", "heading", "title", "label_for"),
    (
        ("/login/", "欢迎回到健康之家", "登录｜健康之家", "id_password"),
        ("/login/first-use/", "第一次使用健康之家", "第一次使用｜健康之家", "id_phone"),
        ("/login/forgot-password/", "重新设置密码", "重新设置密码｜健康之家", "id_phone"),
    ),
)
def test_public_auth_pages_use_approved_copy_title_brand_and_single_heading(
    client,
    path,
    heading,
    title,
    label_for,
):
    response = client.get(path)
    content = response.content.decode()

    assert response.status_code == 200
    assert content.count("<h1") == 1
    assert re.search(rf"<h1[^>]*>{re.escape(heading)}</h1>", content)
    assert f"<title>{title}</title>" in content
    assert "健康之家" in content
    assert "家庭健康档案" in content
    assert f'<label for="{label_for}">' in content


def test_account_deleted_page_explains_safe_state_and_next_action(client):
    response = client.get("/account-deleted/")
    content = response.content.decode()

    assert response.status_code == 200
    assert content.count("<h1") == 1
    assert "全部资料和账号数据正在后台安全清除" in content
    assert "可以关闭此页面" in content
    assert 'href="/login/"' in content


@pytest.mark.django_db
def test_authenticated_shell_has_exact_primary_navigation_task_discovery_and_logout(client, django_user_model):
    account, patient = _account_with_patient(django_user_model, "i", "妈妈")
    client.force_login(account)

    response = client.get("/")
    content = response.content.decode()

    assert response.status_code == 200
    for required in ("<header", "<nav", "<main", 'href="#main-content"'):
        assert required in content
    desktop_navigation = _navigation(content, "desktop-nav")
    mobile_navigation = _navigation(content, "mobile-nav")
    for label in ("首页", "健康档案", "健康趋势", "我的"):
        assert f">{label}<" in desktop_navigation
    for label in ("首页", "档案", "上传", "趋势", "我的"):
        assert f">{label}<" in mobile_navigation
    assert "data-mobile-label" not in content
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
    assert desktop_navigation.count('href="/trends/"') == 1
    assert mobile_navigation.count('href="/trends/"') == 1
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
    tasks = client.get("/tasks/")
    assert tasks.status_code == 200
    tasks_content = tasks.content.decode()
    assert 'class="tasks-page home-page"' in tasks_content
    assert 'aria-labelledby="tasks-title"' in tasks_content


@pytest.mark.parametrize(
    ("template", "title"),
    (
        ("templates/patients/home.html", "首页｜健康之家"),
        ("templates/documents/records.html", "收好的健康资料｜健康之家"),
        ("templates/patients/tasks.html", "处理任务｜健康之家"),
        ("templates/documents/detail.html", "资料详情｜健康之家"),
        ("templates/documents/viewer.html", "查看原件｜健康之家"),
        ("templates/documents/trend.html", "健康趋势｜健康之家"),
        ("templates/documents/delete_confirm.html", "移入回收站｜健康之家"),
    ),
)
def test_authenticated_page_titles_use_current_health_home_brand(template, title):
    source = Path(template).read_text(encoding="utf-8")
    title_block = re.search(r"\{% block title %\}(.*?)\{% endblock %\}", source, re.DOTALL)
    assert title_block is not None
    title_body = title_block.group(1)

    assert f"{{% block title %}}{title}{{% endblock %}}" in source
    source = title_body
    assert "家庭健康资料" not in source


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "desktop_label", "mobile_label"),
    [
        ("/", "首页", "首页"),
        ("/records/", "健康档案", "档案"),
        ("/trends/", "健康趋势", "趋势"),
        ("/uploads/new/", "首页", "上传"),
        ("/me/", "我的", "我的"),
    ],
)
def test_shell_marks_one_current_destination_in_each_responsive_navigation(
    client, django_user_model, path, desktop_label, mobile_label
):
    account, _patient = _account_with_patient(django_user_model, desktop_label + mobile_label)
    client.force_login(account)

    content = client.get(path).content.decode()
    desktop_navigation = _navigation(content, "desktop-nav")
    mobile_navigation = _navigation(content, "mobile-nav")

    assert desktop_navigation.count('aria-current="page"') == 1
    assert mobile_navigation.count('aria-current="page"') == 1
    assert re.search(rf'<a[^>]*aria-current="page"[^>]*>{desktop_label}</a>', desktop_navigation)
    assert re.search(rf'<a[^>]*aria-current="page"[^>]*>{mobile_label}</a>', mobile_navigation)


@pytest.mark.parametrize("path", ["/trends/", "/trends/LAB_WBC/"])
def test_trend_routes_mark_only_trends_current_from_current_section(rf, path):
    request = rf.get(path)
    request.resolver_match = resolve(path)

    content = render_to_string(
        "components/_app_navigation.html",
        {"current_section": "trends"},
        request=request,
    )

    desktop_navigation = _navigation(content, "desktop-nav")
    mobile_navigation = _navigation(content, "mobile-nav")
    assert desktop_navigation.count('aria-current="page"') == 1
    assert mobile_navigation.count('aria-current="page"') == 1
    assert re.search(r'<a[^>]*aria-current="page"[^>]*>健康趋势</a>', desktop_navigation)
    assert re.search(r'<a[^>]*aria-current="page"[^>]*>趋势</a>', mobile_navigation)


def test_app_shell_styles_keep_fixed_navigation_focus_and_responsive_overflow_contract():
    css = Path("static/css/app-shell.css").read_text(encoding="utf-8")
    notifications_css = Path("static/css/notifications.css").read_text(encoding="utf-8")
    javascript = Path("static/js/app-shell.js").read_text(encoding="utf-8")

    assert ":root" not in css
    assert ".app-header" in css and "position: sticky" in css
    assert re.search(r"\.app-header\s*\{[^}]*height:\s*7[2-9]px", css, re.DOTALL)
    assert ".desktop-nav" in css and ".mobile-nav" in css
    assert "position: fixed" in css
    assert "--app-mobile-nav-base-height: 66px" in css
    assert "--app-safe-area-bottom: env(safe-area-inset-bottom, 0px)" in css
    assert "--app-mobile-nav-total-height" in css
    assert "height: var(--app-mobile-nav-total-height)" in css
    assert "padding-bottom: calc(var(--app-mobile-nav-total-height)" in css
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
    assert "content: attr(" not in css
    assert "min-height: 44px" in css
    assert "max-width: 100%" in css
    assert "overflow-x" in css
    assert ":focus-visible" in css
    assert "@media (forced-colors: active)" in css
    current_rule = re.search(
        r':is\(\.desktop-nav, \.mobile-nav\) a\[aria-current="page"\]\s*\{([^}]*)\}',
        css,
    )
    assert current_rule is not None and "text-decoration-line: underline" in current_rule.group(1)
    assert "text-decoration-thickness: 3px" in current_rule.group(1)
    assert "text-underline-offset: .25em" in current_rule.group(1)
    forced_colors_rules = css.split("@media (forced-colors: active)", 1)[1]
    assert not re.search(
        r':is\(\.desktop-nav, \.mobile-nav\) a\[aria-current="page"\]\s*\{'
        r'[^}]*text-decoration',
        forced_colors_rules,
        re.DOTALL,
    )
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "var(--color-paper)" in css
    assert "var(--line)" in css
    assert ":root" not in notifications_css
    assert ".notification-badge[hidden]" in notifications_css
    assert "var(--app-mobile-nav-total-height)" in notifications_css
    assert ".notification-toggle__label" not in notifications_css
    narrow_rules = css.split("@media (max-width: 27rem)", 1)[1].split("@media", 1)[0]
    assert ".brand-lockup__copy" not in narrow_rules
    assert "app-nav-toggle" not in javascript
    assert "data-shell" not in javascript
    assert ".app-sidebar" not in css
    assert not re.search(r"width:\s*1?2?8?0px", css)


def test_public_auth_secondary_links_keep_touch_target_contract():
    css = Path("static/css/public.css").read_text(encoding="utf-8")

    link_rule = re.search(
        r"\.public-main\s+:is\(\.auth-links,\s*\.privacy-copy\)\s+a\s*\{([^}]*)\}",
        css,
        re.DOTALL,
    )
    assert link_rule is not None
    declarations = link_rule.group(1)
    assert re.search(r"display:\s*inline-flex", declarations)
    assert re.search(r"min-height:\s*44px", declarations)
    assert re.search(r"align-items:\s*center", declarations)
