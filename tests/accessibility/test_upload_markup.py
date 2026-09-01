import re
from pathlib import Path

import pytest

from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "upload-markup-test"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.django_db
def test_upload_page_has_an_accessible_file_picker_queue_and_save_boundary(client, django_user_model):
    account = django_user_model.objects.create(phone_hash="u" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "测试患者", CONFIRMATIONS, EVIDENCE)
    client.force_login(account)

    response = client.get("/uploads/new/")
    content = response.content.decode()

    assert response.status_code == 200
    assert content.count("<h1") == 1
    assert 'for="upload-file-input"' in content
    assert 'id="upload-file-input"' in content
    assert 'type="file"' in content
    assert "multiple" in content
    for suffix in (".jpg", ".jpeg", ".png", ".heic", ".pdf"):
        assert suffix in content
    assert re.search(r'<button[^>]*type="submit"[^>]*data-start-upload[^>]*disabled', content)
    assert re.search(r'<button[^>]*type="button"[^>]*data-clear-files[^>]*disabled', content)
    assert 'aria-labelledby="upload-queue-title"' in content
    assert 'aria-labelledby="save-boundary-title"' in content
    assert '<progress data-file-progress max="100" value="0" aria-label="上传进度">' in content
    assert 'role="status" aria-live="polite" aria-atomic="true"' in content
    assert "何时可以离开此页" in content
    assert "原件尚未开始上传" in content
    desktop_navigation = re.search(
        r'<nav class="desktop-nav"[^>]*>(.*?)</nav>', content, re.DOTALL
    ).group(1)
    mobile_navigation = re.search(
        r'<nav class="mobile-nav"[^>]*>(.*?)</nav>', content, re.DOTALL
    ).group(1)
    assert desktop_navigation.count('aria-current="page"') == 1
    assert re.search(r'<a[^>]*aria-current="page"[^>]*>首页</a>', desktop_navigation)
    assert mobile_navigation.count('aria-current="page"') == 1
    assert re.search(r'<a[^>]*aria-current="page"[^>]*>上传</a>', mobile_navigation)
    assert content.count('id="main-content"') == 1


def test_upload_client_enforces_safe_saved_state_polling_and_three_way_concurrency_contract():
    javascript = (PROJECT_ROOT / "static" / "js" / "upload.js").read_text(encoding="utf-8")

    assert "const MAX_CONCURRENT_UPLOADS = 3;" in javascript
    assert "activeUploads < MAX_CONCURRENT_UPLOADS" in javascript
    assert "result.saved === true" in javascript
    assert "setPageCount(row, result.page_count)" in javascript
    assert "result.possible_duplicate === true" in javascript
    assert "可能与已有资料重复" in javascript
    assert "原件已保存" in javascript
    assert "你现在可以离开此页面" in javascript
    assert 'headers["If-None-Match"] = lastEtag' in javascript
    assert "response.status === 304" in javascript
    assert 'window.addEventListener("beforeunload"' in javascript
    assert 'event.returnValue = ""' in javascript
    assert 'window.addEventListener("pagehide"' in javascript
    assert "pollController.abort()" in javascript
    assert "if (result.batch_deleted) resetBatchSession()" in javascript
    assert "`/records/${encodeURIComponent(result.document_id)}/`" in javascript
    assert "textContent" in javascript
    assert "innerHTML" not in javascript
    assert "console." not in javascript


def test_upload_markup_exposes_shared_state_badges_and_partial_failure_contract():
    template = (PROJECT_ROOT / "templates" / "documents" / "upload.html").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "static" / "js" / "upload.js").read_text(encoding="utf-8")

    assert template.count('data-upload-step="') == 3
    assert 'data-upload-step="select"' in template
    assert 'data-upload-step="confirm"' in template
    assert 'data-upload-step="status"' in template
    assert 'class="status-badge' in template
    for label in ("正在上传", "已保存", "自动整理中", "已整理", "仅原件", "处理失败"):
        assert label in javascript
    assert "部分文件未能上传，请查看下面的文件状态。已保存的原件不受影响。" in javascript
    assert re.search(
        r"batchSummary\.textContent = counts\.failed > 0 && counts\.completed > 0\s*"
        r"\? PARTIAL_FAILURE_SUMMARY",
        javascript,
    )
    assert "status.classList.toggle(`status-badge--${key}`, key === badgeKey)" in javascript
    assert "result.hidden = !saved" in javascript
    assert 'error.hidden = !["UPLOAD_FAILED", "PROCESSING_FAILED"].includes(state)' in javascript
    assert "data-file-result" in template
    assert "data-file-error" in template
    assert "batchSummary.textContent" in javascript


def test_upload_styles_preserve_focus_responsive_wrapping_and_narrow_screen_contract():
    css = (PROJECT_ROOT / "static" / "css" / "upload.css").read_text(encoding="utf-8")

    assert ".upload-dropzone:focus-within" in css
    assert "min-width: 0" in css
    assert "overflow-wrap: anywhere" in css
    assert "flex-wrap: wrap" in css
    assert "@media (max-width: 42rem)" in css
    assert "@media (forced-colors: active)" in css
    assert not re.search(r"width:\s*1?2?8?0px", css)
