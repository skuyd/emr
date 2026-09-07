"""Exercise archive navigation and review feedback with a real browser and database."""

import os
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from apps.documents.models import Document, DocumentStatus
from apps.labs.models import LabObservation
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _parsed_document, _patient, _pdf_bytes


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestRecordReviewBrowser(StaticLiveServerTestCase):
    def _prepare(self, marker):
        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        client, patient = _patient(get_user_model(), marker)
        document, _, _ = _parsed_document(patient)
        store = InMemoryObjectStore()
        store.objects[document.original_object_key] = _pdf_bytes()
        return executable, client, document, store

    def _screenshot(self, page, name):
        directory = os.environ.get("PHR_RECORD_REVIEW_ARTIFACT_DIR")
        if directory:
            destination = Path(directory).resolve()
            destination.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(destination / name), full_page=True)

    def test_card_body_title_keyboard_and_original_link_on_desktop_and_phone(self):
        from playwright.sync_api import sync_playwright, expect

        executable, client, document, store = self._prepare("record-navigation-browser")
        with patch("apps.documents.views.originals.get_object_store", lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(locale="zh-CN")
                context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                page = context.new_page()
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(f"{self.live_server_url}/records/", wait_until="networkidle")
                    card = page.locator(".record-card").first
                    title = card.locator("h3 a")
                    expect(title).to_have_attribute("href", f"/records/{document.pk}/")
                    title.focus()
                    page.keyboard.press("Enter")
                    page.wait_for_url(f"{self.live_server_url}/records/{document.pk}/")
                    expect(page.get_by_role("heading", name="自动整理结果", exact=True)).to_be_visible()
                    page.goto(f"{self.live_server_url}/records/", wait_until="networkidle")
                    # A click on card padding should follow the primary title link.
                    card.click(position={"x": 12, "y": 12})
                    page.wait_for_url(f"{self.live_server_url}/records/{document.pk}/")
                    page.goto(f"{self.live_server_url}/records/", wait_until="networkidle")
                    card.get_by_role("link", name="打开原件", exact=True).click()
                    page.wait_for_url(f"{self.live_server_url}/records/{document.pk}/viewer/")
                    expect(page.locator("[data-viewer-image]")).to_have_js_property("complete", True)
                    self.assertGreater(page.locator("[data-viewer-image]").evaluate("image => image.naturalWidth"), 0)
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
            finally:
                browser.close()

    def test_compact_rows_reveal_one_review_action_and_confirmation_survives_reload(self):
        from playwright.sync_api import sync_playwright, expect

        executable, client, document, store = self._prepare("record-review-browser")
        rows = list(LabObservation.objects.filter(parsing_version__document=document)
                    .order_by("document_page__page_number", "reading_order"))
        LabObservation.objects.filter(pk__in=[row.pk for row in rows]).update(quality_issues=[
            {"code": "association_conflict", "label": "字段关联冲突", "fields": ["raw_value"]},
        ])
        with patch("apps.documents.views.originals.get_object_store", lambda: store), \
                patch("apps.labs.views.get_object_store", lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN")
                context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                page = context.new_page()
                errors, failed_assets = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("response", lambda response: failed_assets.append(response.url)
                        if "/static/" in response.url and response.status >= 400 else None)
                page.goto(f"{self.live_server_url}/records/{document.pk}/", wait_until="networkidle")
                expect(page.get_by_role("link", name="核对与修订", exact=True)).to_have_count(0)
                result_rows = page.locator(".observation-list > li")
                expect(result_rows).to_have_count(2)
                self.assertLess(result_rows.first.bounding_box()["height"], 160)
                self._screenshot(page, "detail-desktop.png")

                page.locator(".detail-quality-summary > summary").click()
                expect(page.locator("#quality-association_conflict")).to_contain_text("2 项")
                expect(page.locator("#quality-association_conflict")).to_contain_text("对应关系")
                page.locator(".detail-quality-summary > summary").click()
                result_rows.first.locator("summary").focus()
                page.keyboard.press("Enter")
                expect(page.get_by_role("link", name="核对与修订", exact=True)).to_have_count(1)
                result_rows.nth(1).locator("summary").click()
                expect(result_rows.first.locator("details")).not_to_have_attribute("open", "")
                expect(page.get_by_role("link", name="核对与修订", exact=True)).to_have_count(1)
                result_rows.first.locator("summary").click()
                page.get_by_role("link", name="核对与修订", exact=True).click()
                expect(page.frame_locator('iframe[title="原件对照"]').locator("img")).to_have_js_property("complete", True)
                expect(page.locator(".labs-review-state")).to_contain_text("待核对")
                page.get_by_role("button", name="与原件一致", exact=True).click()
                expect(page.locator(".labs-feedback")).to_contain_text("已保存核对结果")
                expect(page.locator(".labs-review-state")).to_contain_text("已核对")
                expect(page.get_by_role("button", name="已核对", exact=True)).to_be_disabled()
                self._screenshot(page, "review-confirmed-desktop.png")
                page.reload(wait_until="networkidle")
                expect(page.locator(".labs-review-state")).to_contain_text("已核对")
                expect(page.locator(".labs-feedback")).to_have_count(0)
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                self._screenshot(page, "review-confirmed-phone.png")
                page.get_by_role("link", name="返回资料详情", exact=True).click()
                expect(result_rows.first.locator("summary")).to_contain_text("已核对")
                expect(result_rows.first.locator("summary")).to_contain_text("项提示")
                result_rows.first.locator("summary").click()
                expect(page.get_by_role("link", name="核对与修订", exact=True)).to_be_visible()
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                self._screenshot(page, "detail-phone.png")
                self.assertEqual(errors, [])
                self.assertEqual(failed_assets, [])
            finally:
                browser.close()

    def test_card_reprocess_button_remains_an_independent_post_action(self):
        from playwright.sync_api import sync_playwright, expect

        executable, client, document, store = self._prepare("record-reprocess-browser")
        Document.objects.filter(pk=document.pk).update(status=DocumentStatus.PROCESSING_FAILED)
        with patch("apps.documents.views.originals.get_object_store", lambda: store), \
                patch("apps.documents.views.records.safe_enqueue_processing", lambda _: None), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN")
                context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                page = context.new_page()
                page.goto(f"{self.live_server_url}/records/", wait_until="networkidle")
                with page.expect_response(lambda response: response.request.method == "POST"
                                          and response.url.endswith(f"/records/{document.pk}/reprocess/")) as posted:
                    page.locator(".record-card").get_by_role("button", name="重新整理", exact=True).click()
                self.assertEqual(posted.value.status, 302)
                expect(page.locator(".detail-action-success")).to_contain_text("已开始重新整理")
            finally:
                browser.close()
        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.PROCESSING)
