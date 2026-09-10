from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_pathology_browser as original_browser
from tests.browser.test_phase_three_browser import _db
from tests.documents.test_detail_viewer import _patient
from tests.facts.pathology_factories import confirm_graph, ihc_fixture, review


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestPathologyOutputsBrowser(SQLiteSerializedStaticLiveServerTestCase):
    original_store = original_browser.TestPathologyBrowser.original_store
    browser = original_browser.TestPathologyBrowser.browser
    capture = original_browser.TestPathologyBrowser.capture

    def test_phone_selects_cps_with_visible_required_meaning_before_actual_preview(self):
        from playwright.sync_api import expect

        client, patient, document, _, fields = ihc_fixture(get_user_model(), "path-output-phone-preview")
        store = self.original_store(document)
        confirm_graph(patient, fields)
        with self.browser(client, store) as page:
            response = page.goto(self.live_server_url + "/visit/", wait_until="networkidle")
            self.assertEqual(response.status, 200)
            page.get_by_text("选择结构化报告与字段", exact=True).click()
            page.locator('[name="custom_clinical_fields"]').check()
            page.locator(f'[name="clinical_field_ids"][value="{fields["cps"].pk}"]').check()
            for checkbox in page.locator('[name="sections"]').all():
                checkbox.uncheck()
            page.locator('[name="sections"][value="imaging"]').check()
            page.locator('[name="details"]').check()
            self.assertIn("PD-L1 CPS 21", page.locator("main").inner_text())
            self.assertIn("标记、评分和标本/检测归属会随所选结果保留", page.locator("main").inner_text())
            page.get_by_role("button", name="预览内容与导出清单", exact=True).click()
            expect(page.get_by_role("heading", name="确认本次内容", exact=True)).to_be_visible()
            body = page.locator("main").inner_text()
            for text in ("PD-L1 CPS 21", "单位未印刷", "选定标本 1", "选定检测 1", "不可据此判断可比"):
                self.assertIn(text, body)
            for text in ("SYN-CLONE-A", "标本甲", "检测甲", "TPS 13"):
                self.assertNotIn(text, body)
            self.capture(page, "pathology-cps-selected-preview-phone.png")
            _db(lambda: review(patient, fields["clone"], "REVOKE"))
            response = page.reload(wait_until="networkidle")
            self.assertEqual(response.status, 409)
            self.assertNotIn("CPS 21", page.locator("body").inner_text())

    def test_phone_creates_fine_share_without_original_access_and_stops_after_context_change(self):
        from playwright.sync_api import expect

        client, patient, document, _, fields = ihc_fixture(get_user_model(), "path-output-phone-share")
        store = self.original_store(document)
        confirm_graph(patient, fields)
        viewer, _ = _patient(get_user_model(), "path-output-phone-recipient")
        viewer_key = viewer.session.session_key
        with self.browser(client, store) as page:
            response = page.goto(self.live_server_url + f"/patients/{patient.pk}/shares/", wait_until="networkidle")
            self.assertEqual(response.status, 200)
            page.locator(f'[name="document_ids"][value="{document.pk}"]').check()
            page.locator(f'[name="clinical_field_ids"][value="{fields["cps"].pk}"]').check()
            for checkbox in page.locator('[name="sections"]').all():
                checkbox.uncheck()
            page.locator('[name="sections"][value="imaging"]').check()
            page.get_by_role("button", name="生成分享链接", exact=True).click()
            expect(page.locator("#share-link")).to_be_visible()
            link = page.locator("#share-link").input_value()
            recipient_context = page.context.browser.new_context(viewport={"width": 360, "height": 844}, locale="zh-CN")
            recipient_context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": viewer_key, "url": self.live_server_url}])
            recipient_context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
            recipient = recipient_context.new_page()
            errors, failed_assets = [], []
            recipient.on("pageerror", lambda error: errors.append(str(error)))
            recipient.on("response", lambda response: failed_assets.append(response.url) if "/static/" in response.url and response.status >= 400 else None)
            try:
                # Follow the real token-exchange script in a separate session.
                # The landing page navigates again after DOMContentLoaded.
                recipient.goto(link, wait_until="domcontentloaded")
                expect(recipient.get_by_role("heading", name="只读资料分享", exact=True)).to_be_visible()
                expect(recipient.locator("[data-share-content]")).to_contain_text("PD-L1 CPS 21")
                body = recipient.locator("main").inner_text()
                for text in ("PD-L1 CPS 21", "单位未印刷", "选定标本 1", "选定检测 1"):
                    self.assertIn(text, body)
                for text in ("SYN-CLONE-A", "标本甲", "检测甲", "TPS 13"):
                    self.assertNotIn(text, body)
                self.assertEqual(recipient.get_by_role("link", name="查看这份原件", exact=True).count(), 0)
                self.assertEqual(recipient.get_by_role("link", name="下载原件", exact=True).count(), 0)
                self.capture(recipient, "pathology-cps-fine-share-phone.png")
                _db(lambda: review(patient, fields["clone"], "CORRECT", {"value": {"text": "SYN-OTHER"}, "raw_value": "SYN-OTHER"}))
                response = recipient.reload(wait_until="domcontentloaded")
                self.assertEqual(response.status, 410)
                self.assertNotIn("CPS 21", recipient.locator("body").inner_text())
                self.assertEqual(errors, [])
                self.assertEqual(failed_assets, [])
            finally:
                recipient_context.close()
