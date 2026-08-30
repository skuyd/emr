import importlib.util
import os
import secrets
import shutil
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings


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


@override_settings(
    DEBUG=True,
    OTP_PROVIDER="console",
    OTP_FIXED_CODE="123456",
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
)
class TestAc00Ac01Browser(StaticLiveServerTestCase):
    def test_login_onboarding_and_safe_return_load_without_asset_or_console_errors(self):
        if importlib.util.find_spec("playwright") is None:
            self.skipTest("Playwright is not installed")
        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")

        from playwright.sync_api import sync_playwright

        phone = f"139{secrets.randbelow(100_000_000):08d}"
        destination = "/records/?source=browser"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720}, locale="zh-CN")
            console_errors = []
            failed_responses = []
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on(
                "response",
                lambda response: failed_responses.append((response.status, response.url))
                if response.status >= 400
                else None,
            )

            response = page.goto(
                f"{self.live_server_url}/login/?next=%2Frecords%2F%3Fsource%3Dbrowser",
                wait_until="networkidle",
            )
            self.assertEqual(response.status, 200)
            self.assertTrue(page.get_by_role("heading", name="登录").is_visible())
            page.locator("#id_phone").fill(phone)
            page.get_by_role("button", name="获取验证码").click()
            self.assertIn("验证码已发送", page.get_by_role("status").inner_text())
            page.locator("#id_code").fill("123456")
            page.get_by_role("button", name="登录", exact=True).click()
            page.wait_for_url(f"{self.live_server_url}/onboarding/")

            self.assertTrue(page.get_by_role("heading", name="为谁整理资料？").is_visible())
            fields = page.locator("form input:not([name=csrfmiddlewaretoken])").evaluate_all(
                "elements => elements.map(element => element.name).sort()"
            )
            self.assertEqual(
                fields,
                ["display_name", "privacy", "sensitive_data", "upload_authority"],
            )
            page.locator("#id_display_name").fill("浏览器验收")
            for field in ("privacy", "sensitive_data", "upload_authority"):
                page.locator(f"#id_{field}").check()
            page.get_by_role("button", name="开始整理").click()
            page.wait_for_url(f"{self.live_server_url}{destination}")

            self.assertTrue(page.get_by_role("heading", name="病案").is_visible())
            self.assertEqual(page.locator("nav [aria-current=page]").count(), 1)
            self.assertFalse(
                page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                )
            )
            self.assertEqual(failed_responses, [])
            self.assertEqual(console_errors, [])
            browser.close()
