import re
from pathlib import Path

import pytest

from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "home-markup-test"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _rule_declarations(css, selector):
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)}}", css, re.DOTALL)
    assert match, f"Missing CSS rule: {selector}"
    return dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+);", match.group(1), re.IGNORECASE))


@pytest.mark.django_db
def test_home_landmarks_have_one_primary_and_one_mobile_navigation_upload_action(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="h" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "测试患者", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)

    content = client.get("/").content.decode()

    assert content.count("<h1") == 1
    assert re.search(r'<h1 id="home-title">把自己和家人的健康资料，安心收在一起</h1>', content)
    assert 'aria-labelledby="home-tasks-title"' in content
    assert 'aria-labelledby="home-recent-title"' in content
    assert content.count('href="/uploads/new/"') == 2
    assert re.search(
        r'<section[^>]*class="[^"]*home-upload-card[^"]*"[^>]*aria-labelledby="home-upload-title"',
        content,
    )
    assert re.search(
        r'<a[^>]*class="[^"]*button--primary[^"]*"[^>]*href="/uploads/new/"[^>]*>选择图片或 PDF</a>',
        content,
    )
    assert content.count('<li class="home-assurance">') == 3
    assert "这里还没有资料。上传图片或 PDF 后，原件会先安全保存。" in content
    assert "建议使用桌面浏览器" not in content
    assert 'role="status"' in content
    assert 'aria-live="polite"' in content
    assert content.count('aria-current="page"') == 2
    assert re.search(r'<nav class="desktop-nav"[^>]*>.*?aria-current="page">首页</a>', content, re.DOTALL)
    assert re.search(r'<nav class="mobile-nav"[^>]*>.*?aria-current="page">首页</a>', content, re.DOTALL)
    assert content.count('id="main-content"') == 1


def test_home_polling_uses_conditional_safe_dom_updates_and_stops_on_terminal_or_unload():
    javascript = (PROJECT_ROOT / "static" / "js" / "task-status.js").read_text(encoding="utf-8")

    assert 'headers["If-None-Match"] = this.etag' in javascript
    assert "response.status === 304" in javascript
    assert 'this.card.dataset.terminal === "true"' in javascript
    assert "if (!payload.terminal)" in javascript
    assert 'window.addEventListener("pagehide"' in javascript
    assert "controller.abort()" in javascript
    assert "任务状态已更新" in javascript
    assert "textContent" in javascript
    assert "innerHTML" not in javascript
    assert "console." not in javascript


def test_home_styles_use_warm_tokens_and_stack_the_two_column_hero_by_tablet():
    css = (PROJECT_ROOT / "static" / "css" / "home.css").read_text(encoding="utf-8")
    hero = _rule_declarations(css, ".home-hero")
    upload_card = _rule_declarations(css, ".home-upload-card")

    assert hero["display"] == "grid"
    assert hero["grid-template-columns"] == "minmax(0, 1fr)"
    assert upload_card["border-radius"] == "var(--radius-card)"
    assert upload_card["padding"] == "clamp(1.375rem, 2.5vw, 1.5rem)"
    assert "@media (min-width: 64rem)" in css
    assert "grid-template-columns: minmax(0, 1fr) minmax(18rem, .6fr)" in css
    assert "@media (max-width: 47.99rem)" in css
    assert "overflow-wrap: anywhere" in css
    assert "min-width: 0" in css
    assert "@media (forced-colors: active)" in css
    assert not re.search(r"#[0-9a-f]{3,8}\b", css, re.IGNORECASE)
    assert not re.search(r"width:\s*1?2?8?0px", css)


def test_task_item_actions_are_visible_focusable_and_touch_sized():
    css = (PROJECT_ROOT / "static" / "css" / "home.css").read_text(encoding="utf-8")
    action_rules = re.findall(
        r"(?:\.home-task-item-original\s*,\s*\.home-task-item-action|\.home-task-item-action\s*,\s*\.home-task-item-original)\s*\{([^}]+)\}",
        css,
        re.DOTALL,
    )
    assert action_rules
    declarations = next((rule for rule in action_rules if "display" in rule), "")
    assert re.search(r"display:\s*inline-flex", declarations)
    assert re.search(r"min-height:\s*44px", declarations)
    assert re.search(r"align-items:\s*center", declarations)
    forced_colors = css.split("@media (forced-colors: active)", 1)[1]
    assert re.search(r"\.home-task-item-original[^}]*color:\s*LinkText", forced_colors, re.DOTALL)
