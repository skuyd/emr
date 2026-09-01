import importlib.util

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings

from apps.accounts.models import Account
from apps.patients.services import create_patient_space
from tests.browser.test_ac00_ac01_browser import _browser_executable


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestResponsiveShellBrowser(StaticLiveServerTestCase):
    def test_shell_navigation_brand_notifications_and_nonzero_safe_area(self):
        if importlib.util.find_spec("playwright") is None:
            self.skipTest("Playwright is not installed")
        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")

        from playwright.sync_api import sync_playwright

        account = Account.objects.create(phone_hash="s" * 64, phone_encrypted="ciphertext")
        create_patient_space(
            account,
            "妈妈",
            {"privacy": True, "sensitive_data": True, "upload_authority": True},
            {"ip": "127.0.0.1", "user_agent": "shell-browser-test"},
        )
        authenticated = Client()
        authenticated.force_login(account)
        session_cookie = authenticated.cookies[settings.SESSION_COOKIE_NAME].value

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
            context.add_cookies(
                [{"name": settings.SESSION_COOKIE_NAME, "value": session_cookie, "url": self.live_server_url}]
            )
            page = context.new_page()
            console_errors = []
            page_errors = []
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            response = page.goto(f"{self.live_server_url}/", wait_until="networkidle")
            self.assertEqual(response.status, 200)
            self.assertTrue(page.locator(".desktop-nav").is_visible())
            self.assertFalse(page.locator(".mobile-nav").is_visible())
            self.assertEqual(page.locator(".desktop-nav [aria-current=page]").inner_text(), "首页")

            page.set_viewport_size({"width": 390, "height": 844})
            self.assertFalse(page.locator(".desktop-nav").is_visible())
            self.assertTrue(page.locator(".mobile-nav").is_visible())
            self.assertEqual(
                page.locator(".mobile-nav a").all_inner_texts(),
                ["首页", "档案", "上传", "趋势", "我的"],
            )
            self.assertEqual(page.locator(".mobile-nav [aria-current=page]").inner_text(), "首页")

            page.emulate_media(forced_colors="active")
            current_link = page.locator(".mobile-nav:visible [aria-current=page]")
            noncurrent_link = page.locator(".mobile-nav:visible a:not([aria-current=page])").first
            self.assertIn(
                "underline",
                current_link.evaluate("element => getComputedStyle(element).textDecorationLine"),
            )
            self.assertNotIn(
                "underline",
                noncurrent_link.evaluate("element => getComputedStyle(element).textDecorationLine"),
            )
            page.emulate_media(forced_colors="none")

            brand = page.get_by_role("link", name="健康之家", exact=False)
            self.assertTrue(brand.is_visible())
            brand_box = brand.bounding_box()
            self.assertGreaterEqual(brand_box["width"], 44)
            self.assertGreaterEqual(brand_box["height"], 44)

            badge = page.locator("[data-notification-badge]")
            self.assertEqual(badge.evaluate("element => getComputedStyle(element).display"), "none")
            badge.evaluate("element => { element.hidden = false; element.textContent = '2'; }")
            self.assertEqual(badge.evaluate("element => getComputedStyle(element).display"), "grid")
            self.assertTrue(page.get_by_text("通知", exact=True).is_visible())

            page.locator("body").evaluate(
                "element => element.style.setProperty('--app-safe-area-bottom', '20px')"
            )
            mobile_nav = page.locator(".mobile-nav")
            nav_box = mobile_nav.bounding_box()
            self.assertAlmostEqual(nav_box["height"], 86, delta=0.5)
            for target in mobile_nav.locator("a").all():
                target_box = target.bounding_box()
                self.assertGreaterEqual(target_box["height"], 44)
                self.assertLessEqual(target_box["y"] + target_box["height"], 824.5)

            main_padding = page.locator(".app-main").evaluate(
                "element => parseFloat(getComputedStyle(element).paddingBottom)"
            )
            self.assertGreaterEqual(main_padding, 110)

            page.locator("[data-notification-toggle]").click()
            panel_box = page.locator("[data-notification-panel]").bounding_box()
            self.assertLessEqual(panel_box["y"] + panel_box["height"], nav_box["y"])
            toast_bottom = page.locator("[data-notification-toast]").evaluate(
                "element => { element.hidden = false; return parseFloat(getComputedStyle(element).bottom); }"
            )
            self.assertGreaterEqual(toast_bottom, 94)

            self.assertFalse(
                page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            )
            self.assertEqual(console_errors, [])
            self.assertEqual(page_errors, [])
            context.close()
            browser.close()
