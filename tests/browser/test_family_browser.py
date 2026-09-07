from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from unittest.mock import patch

from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.test_detail_viewer import _document, _patient, _png_bytes
from tests.documents.fakes import InMemoryObjectStore


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestFamilyBrowser(StaticLiveServerTestCase):
    def test_desktop_and_mobile_creation_switching_and_old_tab_submission(self):
        from playwright.sync_api import sync_playwright
        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        client, first = _patient(get_user_model(), "browser-family")
        document, _ = _document(first, content_type="image/png", page_count=1)
        store = InMemoryObjectStore()
        store.objects[document.original_object_key] = _png_bytes()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
            context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": client.session.session_key,
                                 "url": self.live_server_url}])
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
            old_tab = context.new_page()
            old_tab.goto(self.live_server_url + "/me/", wait_until="networkidle")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.live_server_url + "/patients/", wait_until="networkidle")
            page.get_by_role("link", name="新增患者", exact=True).click()
            page.get_by_label("患者称呼").fill("浏览器家人")
            page.get_by_label("我确认有权管理这位患者的资料。", exact=True).check()
            page.get_by_role("button", name="创建档案", exact=True).click()
            page.wait_for_url(self.live_server_url + "/")
            self.assertIn("浏览器家人", page.locator(".patient-identity").inner_text())
            old_tab.get_by_label("患者称呼", exact=True).fill("旧页面患者")
            old_tab.get_by_role("button", name="保存称呼", exact=True).click()
            old_tab.wait_for_url(self.live_server_url + f"/me/?name=saved&patient={first.pk}#patient-name")
            self.assertIn("旧页面患者", old_tab.locator(".patient-identity").inner_text())
            with patch("apps.documents.views.originals.get_object_store", return_value=store):
                old_tab.goto(self.live_server_url + f"/records/{document.pk}/viewer/?patient={first.pk}&embed=1", wait_until="networkidle")
                old_tab.wait_for_function("document.querySelector('[data-viewer-image]').naturalWidth > 0")
            page.set_viewport_size({"width": 360, "height": 780})
            page.get_by_role("link", name="切换患者", exact=True).click()
            page.get_by_role("button", name="打开 旧页面患者 的档案", exact=True).click()
            page.wait_for_url(self.live_server_url + "/")
            self.assertIn("旧页面患者", page.locator(".app-patient-context").inner_text())
            box = page.get_by_role("link", name="切换患者", exact=True).bounding_box()
            self.assertGreaterEqual(box["x"], 0)
            self.assertLessEqual(box["x"] + box["width"], 360)
            self.assertEqual(errors, [])
            browser.close()
        from apps.patients.models import Patient
        first.refresh_from_db()
        self.assertEqual(first.display_name, "旧页面患者")
        self.assertTrue(Patient.objects.filter(account=first.account, display_name="浏览器家人").exists())
