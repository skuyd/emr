import hashlib
import io
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections
from django.test import override_settings

from apps.documents.backends import get_object_store
from apps.documents.deletion import DeletionOutcome, purge_document_deletion
from apps.documents.models import Document
from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from apps.facts.models import Fact
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


def _db(action):
    def execute():
        close_old_connections()
        try:
            return action()
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(execute).result(timeout=20)


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestPhaseThreeBrowser(StaticLiveServerTestCase):
    def test_fact_original_preview_export_and_recycle_flow_on_desktop_and_phone(self):
        from playwright.sync_api import sync_playwright, expect
        from reportlab.pdfgen.canvas import Canvas
        from apps.exports.pdf import _font, FONT

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        client, patient = _patient(get_user_model(), "phase-three-browser")
        text = "诊断：考虑炎症；未见明确转移。"
        document, version = parsed_facts(patient, [text], polygons=False)
        fact = Fact.objects.get(parsing_version=version)
        _font()
        output = io.BytesIO()
        canvas = Canvas(output)
        canvas.setFont(FONT, 16)
        canvas.drawString(45, 720, "合成报告，仅供软件验收")
        canvas.drawString(45, 680, text)
        canvas.save()
        payload = output.getvalue()
        document.sha256, document.byte_size = hashlib.sha256(payload).hexdigest(), len(payload)
        Document.objects.filter(pk=document.pk).update(sha256=document.sha256, byte_size=document.byte_size)
        evidence_dir = Path(os.environ["PHR_PHASE_THREE_ARTIFACT_DIR"]).resolve() if os.environ.get("PHR_PHASE_THREE_ARTIFACT_DIR") else None
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phr-phase-three-browser-") as storage_root:
            with override_settings(DOCUMENT_STORAGE_BACKEND="local", DOCUMENT_STORAGE_ROOT=Path(storage_root)):
                store = get_object_store()
                staged = store.put_staging(io.BytesIO(payload), expected_size=len(payload), expected_sha256=document.sha256)
                store.promote_immutable(staged, document.original_object_key)
                with patch("apps.exports.views.safe_enqueue_export", lambda _: None), \
                        patch("apps.documents.views.recycle_bin.safe_enqueue_document_deletion", lambda _: None), \
                        sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN", accept_downloads=True)
                    context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                    page = context.new_page()
                    page_errors, failed_assets = [], []
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on("response", lambda response: failed_assets.append(response.url)
                            if "/static/" in response.url and response.status >= 400 else None)
                    page.route("**/*", lambda route: route.continue_()
                               if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                    try:
                        page.goto(f"{self.live_server_url}/facts/{fact.pk}/", wait_until="networkidle")
                        expect(page.get_by_role("heading", name="原件对照", exact=True)).to_be_visible()
                        expect(page.frame_locator("iframe").locator("[data-viewer-image]")).to_have_js_property("complete", True)
                        page.get_by_label("我已对照原件核对完整摘录及限定表达").check()
                        page.get_by_role("button", name="确认当前内容与原件一致", exact=True).click()
                        expect(page.get_by_role("heading", name="已核对，可纳入就诊资料", exact=True)).to_be_visible()
                        page.goto(f"{self.live_server_url}/visit/", wait_until="networkidle")
                        page.get_by_label("姓名或昵称").fill("合成就诊示例")
                        page.get_by_role("button", name="预览内容与导出清单", exact=True).click()
                        expect(page.get_by_role("heading", name="确认本次内容", exact=True)).to_be_visible()
                        page.wait_for_load_state("networkidle")
                        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                        if evidence_dir:
                            page.screenshot(path=str(evidence_dir / "visit-preview-desktop.png"), full_page=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                        if evidence_dir:
                            page.screenshot(path=str(evidence_dir / "visit-preview-phone.png"), full_page=True)
                        job = _db(lambda: ExportJob.objects.get(patient=patient))
                        pdf_response = context.request.get(f"{self.live_server_url}/visit/{job.pk}/pdf/")
                        self.assertEqual(pdf_response.status, 200)
                        self.assertTrue(pdf_response.body().startswith(b"%PDF"))
                        if evidence_dir:
                            (evidence_dir / "visit-card.pdf").write_bytes(pdf_response.body())
                        page.get_by_label("导出格式").select_option("zip")
                        page.get_by_role("button", name="确认清单并生成", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        _db(lambda: generate_export(job.pk, store))
                        page.reload(wait_until="networkidle")
                        with page.expect_download() as downloading:
                            page.get_by_role("link", name="下载 records.zip", exact=True).click()
                        download = downloading.value
                        self.assertIsNone(download.failure())
                        if evidence_dir:
                            download.save_as(str(evidence_dir / "records.zip"))
                        page.goto(f"{self.live_server_url}/records/{document.pk}/delete/")
                        page.get_by_role("button", name="确认移入回收站", exact=True).click()
                        page.goto(f"{self.live_server_url}/recycle-bin/", wait_until="networkidle")
                        expect(page.get_by_role("heading", name="回收站", exact=True)).to_be_visible()
                        self.assertEqual(context.request.get(f"{self.live_server_url}/visit/{job.pk}/download/").status, 409)
                        page.get_by_role("button", name="恢复 synthetic-report.pdf", exact=True).click()
                        expect(page.get_by_role("status")).to_contain_text("资料已恢复")
                        self.assertEqual(_db(lambda: Fact.objects.get(pk=fact.pk).revision_number), 1)
                        page.goto(f"{self.live_server_url}/records/{document.pk}/delete/")
                        page.get_by_role("button", name="确认移入回收站", exact=True).click()
                        page.goto(f"{self.live_server_url}/recycle-bin/")
                        page.get_by_role("link", name="彻底删除 synthetic-report.pdf", exact=True).click()
                        page.get_by_role("button", name="确认彻底删除，不可恢复", exact=True).click()
                        expect(page.get_by_role("status")).to_contain_text("永久删除已开始")
                        def purge():
                            current = Document.objects.get(pk=document.pk)
                            return purge_document_deletion(current.deletion_job.pk, store).outcome
                        self.assertEqual(_db(purge), DeletionOutcome.PURGED)
                        self.assertFalse(_db(lambda: Document.objects.filter(pk=document.pk).exists()))
                        self.assertEqual(page_errors, [])
                        self.assertEqual(failed_assets, [])
                    finally:
                        browser.close()
