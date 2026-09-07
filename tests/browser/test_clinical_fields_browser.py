import hashlib
import io
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from apps.documents.backends import get_object_store
from apps.documents.models import Document
from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.models import Fact
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.facts.test_clinical_foundation import CT


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestClinicalFieldsBrowser(StaticLiveServerTestCase):
    def test_original_field_correction_selection_and_zip_on_desktop_and_phone(self):
        from playwright.sync_api import sync_playwright, expect
        from reportlab.pdfgen.canvas import Canvas
        from apps.exports.pdf import _font, FONT

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser")
        client, patient = _patient(get_user_model(), "clinical-browser")
        document, version = parsed_facts(patient, CT, document_type="IMAGING")
        _font()
        output = io.BytesIO()
        canvas = Canvas(output)
        canvas.setFont(FONT, 12)
        for index, line in enumerate(CT):
            canvas.drawString(25, 750 - index * 65, line)
        canvas.save()
        payload = output.getvalue()
        document.sha256, document.byte_size = hashlib.sha256(payload).hexdigest(), len(payload)
        Document.objects.filter(pk=document.pk).update(sha256=document.sha256, byte_size=document.byte_size)
        version.document = document
        extract_clinical_version(version)
        fact = Fact.objects.get(parsing_version=version, field_key="imaging.impression")
        evidence_dir = Path(os.environ["PHR_B3_ARTIFACT_DIR"]).resolve() if os.environ.get("PHR_B3_ARTIFACT_DIR") else None
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phr-clinical-browser-") as storage_root:
            with override_settings(DOCUMENT_STORAGE_BACKEND="local", DOCUMENT_STORAGE_ROOT=Path(storage_root)):
                store = get_object_store()
                staged = store.put_staging(io.BytesIO(payload), expected_size=len(payload), expected_sha256=document.sha256)
                store.promote_immutable(staged, document.original_object_key)
                with patch("apps.exports.views.safe_enqueue_export", lambda _: None), sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN", accept_downloads=True)
                    context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                    try:
                        page.goto(f"{self.live_server_url}/facts/{fact.pk}/", wait_until="networkidle")
                        expect(page.get_by_role("heading", name="核对报告结论", exact=True)).to_be_visible()
                        expect(page.frame_locator("iframe").locator("[data-viewer-image]")).to_have_js_property("complete", True)
                        page.get_by_label("我已对照原件核对字段值、部位、单位和限定表达").check()
                        page.get_by_role("button", name="确认当前字段与原件一致", exact=True).click()
                        expect(page.get_by_role("heading", name="已核对，可纳入就诊资料", exact=True)).to_be_visible()
                        page.get_by_label("报告结论").fill("双肺结节，合成核对补充。")
                        page.get_by_label("原件字段文字").fill("双肺结节，合成核对补充。")
                        page.get_by_label("我已对照原件核对字段值、部位、单位和限定表达").check()
                        page.get_by_role("button", name="保存更正并确认", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        for size, name in [({"width": 1280, "height": 900}, "desktop"), ({"width": 390, "height": 844}, "phone")]:
                            page.set_viewport_size(size)
                            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                            if evidence_dir:
                                page.screenshot(path=str(evidence_dir / f"clinical-field-{name}.png"), full_page=True)
                        page.goto(f"{self.live_server_url}/visit/", wait_until="networkidle")
                        page.get_by_text("选择结构化报告与字段", exact=True).click()
                        page.get_by_label("自选已核对的报告字段").check()
                        page.locator(f'input[name="clinical_field_ids"][value="{fact.pk}"]').check()
                        page.get_by_role("button", name="预览内容与导出清单", exact=True).click()
                        expect(page.get_by_role("heading", name="确认本次内容", exact=True)).to_be_visible()
                        job = _db(lambda: ExportJob.objects.get(patient=patient))
                        self.assertEqual([f["id"] for f in job.snapshot["clinical_fields"]], [str(fact.pk)])
                        page.get_by_label("导出格式").select_option("zip")
                        page.get_by_role("button", name="确认清单并生成", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        _db(lambda: generate_export(job.pk, store))
                        page.reload(wait_until="networkidle")
                        with page.expect_download() as downloading:
                            page.get_by_role("link", name="下载 records.zip", exact=True).click()
                        self.assertIsNone(downloading.value.failure())
                        if evidence_dir:
                            downloading.value.save_as(str(evidence_dir / "clinical-records.zip"))
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
