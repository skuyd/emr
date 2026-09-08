"""A real browser reviews a scalar range against an immutable synthetic PDF."""

import hashlib
import io
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4
import zipfile

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from apps.documents.backends import get_object_store
from apps.documents.models import Document
from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.models import Fact
from apps.self_records.services import create_record
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.self_records.test_payloads import payload as record_payload


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestImagingQuantitativeBrowser(StaticLiveServerTestCase):
    def test_range_correction_selected_zip_share_and_revocation(self):
        from playwright.sync_api import sync_playwright, expect
        from reportlab.pdfgen.canvas import Canvas
        from apps.exports.pdf import _font, FONT

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser")
        client, patient = _patient(get_user_model(), "imaging-browser-owner")
        reader_client, _ = _patient(get_user_model(), "imaging-browser-reader")
        daily = create_record(patient, patient.account, record_payload(notes="selected daily note"), creation_key=uuid4()).record
        create_record(patient, patient.account, record_payload(notes="UNSELECTED_DAILY_CANARY"), creation_key=uuid4())
        texts = [
            "合成医院 PET/CT诊断报告书",
            "检查日期：2026-08-17 检查项目：全身PET/CT",
            "影像表现：双肺结节，较大者位于左肺上叶，约12mm，SUVmax≤4.20。",
            "诊断意见：对比前片（2025年06月）：左肺结节同前。PRIVATE_COMPARISON_CANARY。",
            "报告日期：2026-08-18 报告医师：合成医师",
        ]
        document, version = parsed_facts(patient, texts, document_type="IMAGING")
        _font()
        output = io.BytesIO()
        canvas = Canvas(output)
        canvas.setFont(FONT, 10)
        for index, line in enumerate(texts):
            # The synthetic source deliberately differs from its OCR so the
            # browser must correct both numeric bounds and the qualifier.
            canvas.drawString(25, 750 - index * 65, line.replace("SUVmax≤4.20", "SUVmax约3.5～4.0"))
        canvas.save()
        payload = output.getvalue()
        document.sha256, document.byte_size = hashlib.sha256(payload).hexdigest(), len(payload)
        Document.objects.filter(pk=document.pk).update(sha256=document.sha256, byte_size=document.byte_size)
        version.document = document
        extract_clinical_version(version)
        field = Fact.objects.get(parsing_version=version, field_key="lesion.suvmax")
        reference = Fact.objects.get(parsing_version=version, field_key="comparison.reference_date")
        maximum = Fact.objects.get(parsing_version=version, field_key="lesion.maximum_scope")
        evidence = os.environ.get("PHR_B3_IMAGING_ARTIFACT_DIR")
        evidence_dir = Path(evidence).resolve() if evidence else None
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phr-imaging-browser-") as storage_root:
            with override_settings(DOCUMENT_STORAGE_BACKEND="local", DOCUMENT_STORAGE_ROOT=Path(storage_root)):
                store = get_object_store()
                staged = store.put_staging(io.BytesIO(payload), expected_size=len(payload), expected_sha256=document.sha256)
                store.promote_immutable(staged, document.original_object_key)
                with patch("apps.exports.views.safe_enqueue_export", lambda _: None), sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN", accept_downloads=True)
                    context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    try:
                        page.goto(f"{self.live_server_url}/facts/{reference.pk}/", wait_until="networkidle")
                        expect(page.get_by_text("尚未关联到另一份检查", exact=False)).to_be_visible()
                        page.goto(f"{self.live_server_url}/facts/{maximum.pk}/", wait_until="networkidle")
                        expect(page.get_by_text("不能据此选为全报告最大病灶", exact=False)).to_be_visible()
                        page.goto(f"{self.live_server_url}/facts/{field.pk}/", wait_until="networkidle")
                        expect(page.frame_locator("iframe").locator("[data-viewer-image]")).to_have_js_property("complete", True)
                        expect(page.get_by_label("原文数值或范围下限")).to_have_value("4.20")
                        expect(page.get_by_label("原文数值关系")).to_have_value("LE")
                        page.get_by_label("原文数值或范围下限").fill("3.5")
                        page.get_by_label("范围上限（只有原文写明范围时填写）").fill("4.0")
                        page.get_by_label("原文数值关系").select_option("RANGE")
                        page.get_by_label("原文包含“约”或类似近似限定").check()
                        page.get_by_label("原件字段文字").fill("SUVmax约3.5～4.0")
                        page.get_by_label("我已对照原件核对字段值、部位、单位和限定表达").check()
                        page.get_by_role("button", name="保存更正并确认", exact=True).click()
                        expect(page.get_by_role("heading", name="已核对，可纳入就诊资料", exact=True)).to_be_visible()
                        for size, name in [({"width": 1280, "height": 900}, "desktop"), ({"width": 390, "height": 844}, "phone")]:
                            page.set_viewport_size(size)
                            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                            if evidence_dir:
                                page.screenshot(path=str(evidence_dir / f"imaging-suv-{name}.png"), full_page=True)
                        page.goto(f"{self.live_server_url}/visit/", wait_until="networkidle")
                        page.get_by_text("选择结构化报告与字段", exact=True).click()
                        page.get_by_label("自选已核对的报告字段").check()
                        page.locator(f'input[name="clinical_field_ids"][value="{field.pk}"]').check()
                        page.locator(f'input[name="self_record_ids"][value="{daily.pk}"]').check()
                        page.get_by_role("button", name="预览内容与导出清单", exact=True).click()
                        job = _db(lambda: ExportJob.objects.get(patient=patient))
                        self.assertEqual([f["id"] for f in job.snapshot["clinical_fields"]], [str(field.pk)])
                        self.assertEqual(job.snapshot["schema_version"], "1.2")
                        self.assertEqual([r["id"] for r in job.snapshot["self_records"]], [str(daily.pk)])
                        value = job.snapshot["clinical_fields"][0]["content"]["value"]
                        self.assertEqual(value["values"], ["3.5", "4.0"])
                        self.assertEqual(value["comparator"], "RANGE")
                        self.assertTrue(value["approximate"])
                        self.assertIsNone(value["unit"])
                        page.get_by_label("导出格式").select_option("zip")
                        page.get_by_role("button", name="确认清单并生成", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        _db(lambda: generate_export(job.pk, store))
                        page.reload(wait_until="networkidle")
                        with page.expect_download() as downloading:
                            page.get_by_role("link", name="下载 records.zip", exact=True).click()
                        download = downloading.value
                        self.assertIsNone(download.failure())
                        with zipfile.ZipFile(download.path()) as archive:
                            structured = "\n".join(archive.read(name).decode("utf-8-sig") for name in archive.namelist() if name.endswith((".json", ".csv")))
                        self.assertIn("3.5", structured)
                        self.assertNotIn("PRIVATE_COMPARISON_CANARY", structured)
                        self.assertNotIn("4.20", structured)
                        self.assertIn("selected daily note", structured)
                        self.assertNotIn("UNSELECTED_DAILY_CANARY", structured)
                        if evidence_dir:
                            download.save_as(str(evidence_dir / "imaging-selected-records.zip"))
                        page.goto(f"{self.live_server_url}/patients/{patient.pk}/shares/", wait_until="networkidle")
                        page.locator(f'input[name="document_ids"][value="{document.pk}"]').check()
                        for checkbox in page.locator('input[name="sections"]').all():
                            checkbox.uncheck()
                        page.locator('input[name="sections"][value="imaging"]').check()
                        page.locator('input[name="sections"][value="self_records"]').check()
                        page.locator(f'input[name="self_record_ids"][value="{daily.pk}"]').check()
                        page.locator(f'input[name="clinical_field_ids"][value="{field.pk}"]').check()
                        page.get_by_role("button", name="生成分享链接", exact=True).click()
                        share_link = page.locator("#share-link").input_value()
                        reader_context = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN")
                        reader_context.add_cookies([{"name": "sessionid", "value": reader_client.session.session_key, "url": self.live_server_url}])
                        reader_context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                        reader = reader_context.new_page()
                        reader.on("pageerror", lambda error: errors.append(str(error)))
                        reader.goto(share_link, wait_until="domcontentloaded")
                        expect(reader.get_by_role("heading", name="只读资料分享", exact=True)).to_be_visible()
                        expect(reader.locator("[data-share-content]")).to_contain_text("3.5")
                        expect(reader.locator("[data-share-content]")).to_contain_text("4.0")
                        expect(reader.locator("[data-share-content]")).not_to_contain_text("PRIVATE_COMPARISON_CANARY")
                        expect(reader.locator("[data-share-content]")).to_contain_text("selected daily note")
                        expect(reader.locator("[data-share-content]")).not_to_contain_text("UNSELECTED_DAILY_CANARY")
                        self.assertEqual(reader.get_by_role("link", name="查看这份原件", exact=True).count(), 0)
                        self.assertNotIn("#", reader.url)
                        self.assertFalse(reader.evaluate("document.documentElement.scrollWidth > innerWidth"))
                        if evidence_dir:
                            reader.screenshot(path=str(evidence_dir / "imaging-suv-share-phone.png"), full_page=True)
                        page.goto(f"{self.live_server_url}/facts/{field.pk}/", wait_until="networkidle")
                        page.get_by_role("button", name="撤销确认", exact=True).click()
                        response = reader.reload(wait_until="networkidle")
                        self.assertEqual(response.status, 410)
                        self.assertNotIn("3.5", reader.locator("main").inner_text())
                        self.assertNotIn("selected daily note", reader.locator("main").inner_text())
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
