import re
from pathlib import Path

import pytest

from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "home-markup-test"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.django_db
def test_home_landmarks_have_one_primary_and_one_mobile_navigation_upload_action(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="h" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "测试患者", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)

    content = client.get("/").content.decode()

    assert content.count("<h1") == 1
    assert 'id="home-title"' in content
    assert 'aria-labelledby="home-tasks-title"' in content
    assert 'aria-labelledby="home-recent-title"' in content
    assert content.count('href="/uploads/new/"') == 2
    assert re.search(r'<a[^>]*class="home-upload-primary"[^>]*>上传资料</a>', content)
    assert "还没有资料。上传检查单、报告图片或 PDF，系统会自动帮你整理。" in content
    assert 'role="status"' in content
    assert 'aria-live="polite"' in content
    assert content.count('aria-current="page"') == 1
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


def test_home_styles_have_exact_desktop_two_column_and_narrow_single_column_contracts():
    css = (PROJECT_ROOT / "static" / "css" / "home.css").read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert "@media (min-width: 80rem)" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "@media (max-width: 47.99rem)" in css
    assert "@media (max-width: 63.99rem)" in css
    assert ".home-desktop-notice { display: block; }" in css
    assert "overflow-wrap: anywhere" in css
    assert "min-width: 0" in css
    assert "@media (forced-colors: active)" in css
    assert not re.search(r"width:\s*1?2?8?0px", css)
