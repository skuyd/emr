"""Real browser recovery from a non-document suggestion, including native source IO."""
import hashlib
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from apps.documents.models import Document, DocumentPage, ProcessingRun, UploadItem
from apps.patients.models import Patient
from apps.processing.models import MaterialDecision, ParsingVersion
from apps.processing.runner import run_processing
from apps.processing.value_objects import OcrPage
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_material_views import material_document
from tests.processing.test_image_enhancement import encoded
from tests.processing.test_material_classification import synthetic_scene
from tests.processing.test_pipeline import _pipeline


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestMaterialBrowser(StaticLiveServerTestCase):
    def test_task_opened_before_document_exists_gains_scoped_recovery_link(self):
        from playwright.sync_api import sync_playwright, expect

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        client, document, _version = material_document(get_user_model(), "material-late-document")
        item = document.batch.items.get()
        UploadItem.objects.filter(pk=item.pk).update(document=None, status="UPLOADING")
        second = Patient.objects.create(account=document.patient.account, display_name="Second patient")
        self.assertEqual(client.post(f"/patients/{second.pk}/select/").status_code, 302)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={"width": 360, "height": 900})
                context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                page = context.new_page()

                def completed(route):
                    self.assertEqual(route.request.headers.get("x-patient-id"), str(document.patient_id))
                    route.fulfill(json={
                        "terminal": True, "counts": {"processing": 0, "completed": 1, "failed": 0, "total": 1},
                        "items": [{"item_id": str(item.pk), "status": "ORIGINAL_ONLY", "document_id": str(document.pk),
                                   "material": {"label": "可能不是单据"}}],
                    })

                page.route(f"**/api/upload-batches/{document.batch_id}/status/", completed)
                page.goto(f"{self.live_server_url}/tasks/?patient={document.patient_id}", wait_until="networkidle")
                card = page.locator("[data-task-card]")
                card.get_by_text("查看详情", exact=True).click()
                expect(card.locator("[data-material-label]")).to_have_text("可能不是单据")
                link = card.locator("[data-material-link]")
                expect(link).to_be_visible(timeout=3000)
                expect(link).to_have_count(1)
                expect(link).to_have_attribute("href", f"/records/{document.pk}/#material-review")
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                link.click()
                expect(page.locator("#material-review")).to_be_visible()
                # A resource URL resolves its immutable document patient even
                # though another tab selected the second patient in the session.
                expect(page.locator('meta[name="patient-id"]')).to_have_attribute("content", str(document.patient_id))
            finally:
                browser.close()

    def test_task_polling_shows_material_hint_and_recovery_link_after_ocr(self):
        from playwright.sync_api import sync_playwright, expect

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        client, document, version = material_document(get_user_model(), "material-task-browser")
        Document.objects.filter(pk=document.pk).update(status="PROCESSING")
        ParsingVersion.objects.filter(pk=version.pk).update(diagnostics={})
        item_id = str(document.batch.items.get().pk)
        session_key = client.session.session_key
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={"width": 360, "height": 900})
                context.add_cookies([{"name": "sessionid", "value": session_key, "url": self.live_server_url}])
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url) else route.abort())
                page = context.new_page()
                page.route(f"**/api/upload-batches/{document.batch_id}/status/", lambda route: route.fulfill(json={
                    "terminal": True,
                    "counts": {"processing": 0, "completed": 1, "failed": 0, "total": 1},
                    "items": [{"item_id": item_id, "status": "ORIGINAL_ONLY", "document_id": str(document.pk), "material": {"label": "可能不是单据"}}],
                }))
                page.goto(f"{self.live_server_url}/tasks/", wait_until="networkidle")
                card = page.locator("[data-task-card]")
                card.get_by_text("查看详情", exact=True).click()
                expect(card.locator("[data-material-label]")).to_have_text("可能不是单据")
                expect(card.locator("[data-material-link]")).to_be_visible()
                expect(card.locator("[data-material-link]")).to_have_count(1)
                expect(card.locator("[data-material-link]")).to_have_attribute("href", f"/records/{document.pk}/#material-review")
                expect(card.locator('[data-task-item-help="PROCESSING_FAILED"]')).to_be_hidden()
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
            finally:
                browser.close()

    def test_original_keep_reprocess_reset_survive_reload_on_desktop_and_phone(self):
        from playwright.sync_api import sync_playwright, expect

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        payload = encoded(synthetic_scene())
        digest = hashlib.sha256(payload).hexdigest()
        store = InMemoryObjectStore()
        provider_page = OcrPage(1, 640, 480, (), "fixture", "1")
        queued = []
        scenarios = []
        for width in (1440, 360):
            client, document, version = material_document(get_user_model(), f"material-browser-{width}")
            Document.objects.filter(pk=document.pk).update(sha256=digest, byte_size=len(payload))
            DocumentPage.objects.filter(document=document).update(width=640, height=480)
            diagnostics = version.diagnostics
            diagnostics["material"]["source_sha256"] = digest
            ParsingVersion.objects.filter(pk=version.pk).update(diagnostics=diagnostics)
            store.objects[document.original_object_key] = payload
            scenarios.append((width, client.session.session_key, document))

        def dispatch(run_id):
            queued.append(run_id)
            run_processing(run_id, _pipeline(store, provider_page))

        with patch("apps.documents.views.originals.get_object_store", lambda: store), \
                patch("apps.documents.views.records.safe_enqueue_processing", dispatch), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                for width, session_key, document in scenarios:
                    context = browser.new_context(viewport={"width": width, "height": 900}, locale="zh-CN")
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url) else route.abort())
                    context.add_cookies([{"name": "sessionid", "value": session_key, "url": self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"{self.live_server_url}/records/", wait_until="networkidle")
                    card = page.locator(".record-card").filter(has_text=document.display_filename).first
                    expect(card.locator("[data-material-label]")).to_contain_text("可能不是单据")
                    card.locator("h3 a").click()
                    section = page.locator("#material-review")
                    expect(section).to_contain_text("原件已完整保存")
                    image = page.frame_locator('iframe[title="原始报告预览"]').locator("img[data-viewer-image]")
                    expect(image).to_have_js_property("complete", True)
                    self.assertGreater(image.evaluate("image => image.naturalWidth"), 0)
                    section.get_by_role("button", name="按资料保留并重新整理", exact=True).click()
                    expect(section.get_by_role("heading")).to_have_text("已按资料保留")
                    page.reload(wait_until="networkidle")
                    expect(section.get_by_role("heading")).to_have_text("已按资料保留")
                    section.get_by_role("button", name="恢复自动判断", exact=True).click()
                    expect(section.get_by_role("heading")).to_have_text("可能不是单据")
                    page.reload(wait_until="networkidle")
                    expect(section.get_by_role("heading")).to_have_text("可能不是单据")
                    self.assertEqual(hashlib.sha256(store.objects[document.original_object_key]).hexdigest(), digest)
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    self.assertEqual(errors, [])
                    context.close()
            finally:
                browser.close()
        self.assertEqual(len(queued), 2)
        for _width, _session, document in scenarios:
            self.assertEqual(ProcessingRun.objects.filter(document=document).count(), 2)
            self.assertEqual(MaterialDecision.objects.filter(document=document).count(), 2)
            self.assertEqual(ParsingVersion.objects.filter(document=document).count(), 2)
