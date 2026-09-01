import importlib.util
import io
import os
import secrets
import shutil
import tempfile
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from PIL import Image

from apps.documents.models import Document, ProcessingRun, UploadItem, UploadItemStatus


OTP_CODE = "230412"
FIRST_USE_PASSWORD = "Strong upload browser passphrase 2026"


def _browser_executable():
    configured = os.environ.get("PHR_BROWSER_EXECUTABLE")
    candidates = [
        configured,
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    return next((Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)


def _png_bytes(color="#4f766f"):
    output = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(output, format="PNG")
    return output.getvalue()


def _first_use(page, base_url, phone, code, password):
    page.goto(f"{base_url}/login/", wait_until="networkidle")
    page.get_by_role("link", name="第一次使用健康之家", exact=True).click()
    page.wait_for_url(f"{base_url}/login/first-use/")
    page.locator("#id_phone").fill(phone)
    page.get_by_role("button", name="发送验证码", exact=True).click()
    page.wait_for_url(f"{base_url}/login/first-use/verify/")
    page.locator("#id_code").fill(code)
    page.get_by_role("button", name="验证手机号", exact=True).click()
    page.wait_for_url(f"{base_url}/login/first-use/password/")
    page.locator("#id_password1").fill(password)
    page.locator("#id_password2").fill(password)
    page.get_by_role("button", name="设置密码", exact=True).click()


@override_settings(
    DEBUG=True,
    OTP_PROVIDER="development",
    OTP_FIXED_CODE=OTP_CODE,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
)
class TestAc02UploadBrowser(StaticLiveServerTestCase):
    def test_authenticated_user_uploads_a_real_image_without_console_asset_or_overflow_errors(self):
        if importlib.util.find_spec("playwright") is None:
            self.skipTest("Playwright is not installed")
        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")

        from playwright.sync_api import sync_playwright

        phone = f"139{secrets.randbelow(100_000_000):08d}"
        with tempfile.TemporaryDirectory(prefix="phr-upload-browser-") as object_root:
            with override_settings(DOCUMENT_STORAGE_BACKEND="local", DOCUMENT_STORAGE_ROOT=Path(object_root)):
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                    page = browser.new_page(viewport={"width": 1280, "height": 720}, locale="zh-CN")
                    console_errors = []
                    page_errors = []
                    failed_responses = []
                    failed_requests = []
                    page.on(
                        "console",
                        lambda message: console_errors.append(message.text)
                        if message.type == "error"
                        else None,
                    )
                    page.on(
                        "pageerror",
                        lambda error: page_errors.append(str(error)),
                    )
                    page.on(
                        "response",
                        lambda response: failed_responses.append((response.status, response.url))
                        if response.status >= 400
                        else None,
                    )
                    page.on(
                        "requestfailed",
                        lambda request: failed_requests.append(
                            (request.method, request.url, request.failure)
                        ),
                    )

                    _first_use(
                        page,
                        self.live_server_url,
                        phone,
                        OTP_CODE,
                        FIRST_USE_PASSWORD,
                    )
                    page.wait_for_url(f"{self.live_server_url}/onboarding/")
                    page.locator("#id_display_name").fill("浏览器验收")
                    for field in ("privacy", "sensitive_data", "upload_authority"):
                        page.locator(f"#id_{field}").check()
                    page.get_by_role("button", name="开始整理").click()
                    page.wait_for_url(f"{self.live_server_url}/")

                    response = page.goto(f"{self.live_server_url}/uploads/new/", wait_until="networkidle")
                    self.assertEqual(response.status, 200)
                    self.assertTrue(page.get_by_role("heading", name="上传资料", exact=True).is_visible())
                    self.assertEqual(page.locator("nav:visible [aria-current=page]").count(), 1)
                    self.assertEqual(
                        page.locator(".desktop-nav:visible [aria-current=page]").inner_text(),
                        "首页",
                    )
                    self.assertFalse(
                        page.evaluate(
                            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                        )
                    )

                    page.locator("#upload-file-input").set_input_files(
                        {"name": "unsupported.exe", "mimeType": "application/octet-stream", "buffer": b"invalid"}
                    )
                    with page.expect_response(
                        lambda candidate: candidate.request.method == "POST"
                        and candidate.url.endswith("/api/upload-batches/")
                    ) as batch_response:
                        page.get_by_role("button", name="开始上传").click()
                    self.assertEqual(batch_response.value.status, 201)
                    with page.expect_response(
                        lambda candidate: candidate.request.method == "POST"
                        and candidate.url.endswith("/remove/")
                    ) as remove_response:
                        page.get_by_role("button", name="移除").click()
                    self.assertEqual(remove_response.value.status, 200)
                    self.assertEqual(page.locator("[data-file-row]").count(), 0)
                    self.assertFalse(page.locator("#upload-file-input").is_disabled())

                    page.locator("#upload-file-input").set_input_files(
                        {"name": "synthetic-check.png", "mimeType": "image/png", "buffer": _png_bytes()}
                    )
                    self.assertIn("已选择 1 个文件", page.locator("[data-selection-summary]").inner_text())
                    page.get_by_role("button", name="开始上传").click()
                    page.locator('[data-file-status][data-state="PROCESSING"]').wait_for(timeout=15_000)
                    self.assertIn("原件已保存", page.locator("[data-leave-notice]").inner_text())
                    self.assertIn("可以离开", page.locator("[data-leave-notice]").inner_text())

                    page.goto(f"{self.live_server_url}/uploads/new/", wait_until="networkidle")
                    page.locator("#upload-file-input").set_input_files(
                        {
                            "name": "synthetic-near.png",
                            "mimeType": "image/png",
                            "buffer": _png_bytes("#d97706"),
                        }
                    )
                    page.get_by_role("button", name="开始上传").click()
                    page.locator("[data-possible-duplicate]").wait_for(state="visible", timeout=15_000)
                    self.assertIn("可能与已有资料重复", page.locator("[data-possible-duplicate]").inner_text())

                    home_response = page.goto(f"{self.live_server_url}/", wait_until="networkidle")
                    self.assertEqual(home_response.status, 200)
                    self.assertTrue(page.get_by_role("heading", name="首页", exact=True).is_visible())
                    self.assertEqual(page.get_by_role("link", name="上传资料", exact=True).count(), 1)
                    self.assertTrue(
                        page.locator(".home-recent-name", has_text="synthetic-check.png").is_visible()
                    )
                    self.assertEqual(page.locator("[data-task-card]").count(), 2)
                    first_task_card = page.locator("[data-task-card]").first
                    first_task_card.locator("summary").click()
                    self.assertTrue(first_task_card.locator("[data-task-item-id]").is_visible())
                    self.assertEqual(first_task_card.locator("[data-task-item-id]").inner_text(), "处理中")
                    desktop_columns = page.locator(".home-layout").evaluate(
                        "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
                    )
                    self.assertEqual(desktop_columns, 2)
                    self.assertFalse(
                        page.evaluate(
                            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                        )
                    )
                    page.set_viewport_size({"width": 1440, "height": 900})
                    wide_columns = page.locator(".home-layout").evaluate(
                        "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
                    )
                    self.assertEqual(wide_columns, 2)
                    self.assertFalse(
                        page.evaluate(
                            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                        )
                    )
                    page.set_viewport_size({"width": 1024, "height": 720})
                    narrow_columns = page.locator(".home-layout").evaluate(
                        "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
                    )
                    self.assertEqual(narrow_columns, 1)
                    self.assertFalse(
                        page.evaluate(
                            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                        )
                    )
                    page.set_viewport_size({"width": 1023, "height": 720})
                    self.assertTrue(page.locator(".home-desktop-notice").is_visible())

                    page.set_viewport_size({"width": 1280, "height": 720})
                    page.goto(f"{self.live_server_url}/", wait_until="networkidle")
                    page.locator(
                        ".home-recent-card", has_text="synthetic-check.png"
                    ).click()
                    page.wait_for_url(f"{self.live_server_url}/records/**")
                    preview_image = page.frame_locator(
                        'iframe[name="document-preview"]'
                    ).locator("[data-viewer-image]")
                    preview_image.wait_for(state="attached", timeout=15_000)
                    image_metrics = preview_image.evaluate(
                        """async image => {
                            if (!image.complete) {
                                await new Promise((resolve, reject) => {
                                    image.addEventListener("load", resolve, {once: true});
                                    image.addEventListener("error", reject, {once: true});
                                });
                            }
                            const imageBounds = image.getBoundingClientRect();
                            const stageBounds = image.closest("[data-viewer-stage]").getBoundingClientRect();
                            return {
                                naturalWidth: image.naturalWidth,
                                imageWidth: imageBounds.width,
                                imageHeight: imageBounds.height,
                                stageWidth: stageBounds.width,
                            };
                        }"""
                    )
                    self.assertGreater(image_metrics["naturalWidth"], 0)
                    self.assertGreater(image_metrics["stageWidth"], 0)
                    self.assertGreater(image_metrics["imageWidth"], 0)
                    self.assertGreater(image_metrics["imageHeight"], 0)

                    self.assertEqual(failed_responses, [])
                    self.assertEqual(failed_requests, [])
                    self.assertEqual(page_errors, [])
                    self.assertEqual(console_errors, [])
                    browser.close()

                documents = list(Document.objects.order_by("created_at", "pk"))
                self.assertEqual(len(documents), 2)
                self.assertEqual(documents[0].perceptual_hash, documents[1].perceptual_hash)
                for document in documents:
                    item = UploadItem.objects.get(document=document)
                    run = ProcessingRun.objects.get(document=document)
                    self.assertEqual(item.status, UploadItemStatus.CREATED)
                    self.assertEqual(document.page_count, 1)
                    self.assertTrue((Path(object_root) / document.original_object_key).is_file())
                    self.assertEqual(run.stage, "QUEUED")
