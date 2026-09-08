"""Actual original images, relation decisions and charts in desktop/phone Chromium."""

import hashlib
import io
import os
from pathlib import Path
import re
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from apps.documents.backends import get_object_store
from apps.documents.models import Document
from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from apps.lesions.models import Lesion, LesionMatchProposal, LesionOperation
from apps.lesions.readmodels import review_observations
from tests.browser.sqlite_server import SQLiteSerializedLiveServerThread
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


def source_report(patient, store, day, size, unit, suv):
    from reportlab.pdfgen.canvas import Canvas
    from apps.exports.pdf import _font, FONT

    texts = ["合成医院 PET/CT诊断报告书", f"检查日期：{day} 检查项目：全身PET/CT",
             f"影像表现：左肺上叶见结节，长径{size}{unit}，SUVmax{suv}。",
             "诊断意见：本次最大病灶为左肺上叶结节。"]
    document, version = parsed_facts(patient, texts, document_type="IMAGING")
    _font()
    output = io.BytesIO()
    page = document.pages.get()
    canvas = Canvas(output, pagesize=(page.width, page.height))
    canvas.setFont(FONT, 16)
    for index, line in enumerate(texts):
        # Match parsed_facts' source box [.1, .1+i*.02, .8, .12+i*.02]
        # so actual highlighted pixels enclose the immutable synthetic text.
        canvas.drawString(page.width * .1, page.height * (.88 - index * .02) + 5, line)
    canvas.save()
    payload = output.getvalue()
    document.sha256, document.byte_size = hashlib.sha256(payload).hexdigest(), len(payload)
    Document.objects.filter(pk=document.pk).update(sha256=document.sha256, byte_size=document.byte_size)
    version.document = document
    staged = store.put_staging(io.BytesIO(payload), expected_size=len(payload), expected_sha256=document.sha256)
    store.promote_immutable(staged, document.original_object_key)
    extract_clinical_version(version)
    for field in document.clinical_reports.get().fields.all():
        revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(field)["current_source_token"], checked_original=True)
    return document


def labelled(page, text):
    # Django's label_tag appends the locale-specific colon.
    return page.get_by_label(re.compile("^" + re.escape(text) + "[:：]?$"))


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestLesionRelationsBrowser(StaticLiveServerTestCase):
    server_thread_class = SQLiteSerializedLiveServerThread

    def test_two_originals_decisions_split_undo_and_source_invalidation(self):
        from playwright.sync_api import sync_playwright, expect

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser")
        client, patient = _patient(get_user_model(), "lesion-browser-owner")
        evidence = os.environ.get("PHR_B3_LESION_ARTIFACT_DIR")
        evidence_dir = Path(evidence).resolve() if evidence else None
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="phr-lesion-browser-") as storage_root:
            with override_settings(DOCUMENT_STORAGE_BACKEND="local", DOCUMENT_STORAGE_ROOT=Path(storage_root)):
                store = get_object_store()
                source_report(patient, store, "2026-08-01", "1.2", "cm", "3.2")
                source_report(patient, store, "2026-09-01", "15", "mm", "4.1")
                observations = review_observations(patient, actor=patient.account)
                self.assertEqual(len(observations), 2)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
                    context.add_cookies([{"name": "sessionid", "value": client.session.session_key, "url": self.live_server_url}])
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                    page = context.new_page()
                    errors, server_errors = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("response", lambda response: server_errors.append(response.status) if response.status >= 500 else None)
                    try:
                        page.goto(self.live_server_url + "/lesions/", wait_until="networkidle")
                        page.get_by_role("button", name="按当前报告生成关联提议", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        page.locator('a[href*="/lesions/proposals/"]').click()
                        for index in (0, 1):
                            expect(page.frame_locator("iframe").nth(index).locator("[data-viewer-image]")).to_have_js_property("complete", True)
                        expect(page.locator("main")).to_contain_text("1.2cm")
                        expect(page.locator("main")).to_contain_text("15mm")
                        for viewport, name in [({"width": 1280, "height": 900}, "desktop"), ({"width": 390, "height": 844}, "phone")]:
                            page.set_viewport_size(viewport)
                            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                            if evidence_dir:
                                page.screenshot(path=str(evidence_dir / f"lesion-originals-{name}.png"), full_page=True)
                        page.get_by_role("button", name="暂缓核对", exact=True).click()
                        expect(page.locator("main")).to_contain_text("暂缓")
                        page.get_by_role("button", name="拒绝这条提议", exact=True).click()
                        expect(page.locator("main")).to_contain_text("已拒绝")
                        page.goto(self.live_server_url + "/lesions/", wait_until="networkidle")
                        page.get_by_role("button", name="按当前报告生成关联提议", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        self.assertEqual(_db(lambda: LesionMatchProposal.objects.filter(patient=patient).count()), 1)
                        expect(page.locator("main")).to_contain_text("已拒绝")
                        page.locator('a[href*="/lesions/proposals/"]').click()
                        labelled(page, "新观察名称").fill("肺部观察 A")
                        labelled(page, "我已查看两端原件，确认这些观察使用同一病灶标识").check()
                        page.get_by_role("button", name="确认关联", exact=True).click()
                        expect(page.get_by_role("heading", name="肺部观察 A", exact=True)).to_be_visible()
                        expect(page.locator(".lesion-trend")).to_have_count(2)
                        expect(page.locator(".lesion-trend-segment")).to_have_count(2)
                        expect(page.locator("main")).to_contain_text("1.2 cm × 10 = 12 mm")
                        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                        if evidence_dir:
                            page.screenshot(path=str(evidence_dir / "lesion-trends-phone.png"), full_page=True)
                        lesion = _db(lambda: Lesion.objects.get(patient=patient))
                        page.get_by_text("修改便于辨认的观察名称", exact=True).click()
                        labelled(page, "观察名称").fill("长期观察 A")
                        page.get_by_role("button", name="保存名称", exact=True).click()
                        expect(page.get_by_role("heading", name="长期观察 A", exact=True)).to_be_visible()
                        page.goto(self.live_server_url + "/lesions/", wait_until="networkidle")
                        page.get_by_label("搜索观察名称、标识或当前字段原文", exact=True).fill("长期观察")
                        page.get_by_role("button", name="搜索观察", exact=True).click()
                        expect(page.get_by_role("link", name="长期观察 A", exact=True)).to_have_count(1)
                        page.goto(self.live_server_url + "/records/", wait_until="networkidle")
                        page.get_by_label("搜索资料", exact=True).fill("长期观察")
                        page.get_by_role("button", name="搜索", exact=True).click()
                        expect(page.locator(".record-card")).to_have_count(2)
                        expect(page.get_by_role("link", name="长期观察 A", exact=True)).to_have_count(2)
                        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                        if evidence_dir:
                            page.screenshot(path=str(evidence_dir / "lesion-search-phone.png"), full_page=True)
                        page.get_by_role("link", name="长期观察 A", exact=True).first.click()
                        expect(page.get_by_role("heading", name="长期观察 A", exact=True)).to_be_visible()
                        page.get_by_role("link", name="拆分或取消部分关联", exact=True).click()
                        page.locator(f'input[name="selected"][value="{observations[1]["id"]}"]').check()
                        labelled(page, "拆分后的新名称").fill("独立观察 B")
                        labelled(page, "我已查看原件，确认所选观察应使用独立标识").check()
                        page.get_by_role("button", name="拆分为独立观察", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        operation = _db(lambda: LesionOperation.objects.get(patient=patient, action="SPLIT"))
                        page.goto(f"{self.live_server_url}/lesions/operations/{operation.pk}/", wait_until="networkidle")
                        page.get_by_role("button", name="撤销本次完整操作", exact=True).click()
                        page.wait_for_load_state("networkidle")
                        self.assertTrue(all(row["usable"] for row in _db(lambda: review_observations(patient, actor=patient.account))))
                        site_id = next(field["id"] for field in observations[1]["fields"] if field["field_key"] == "lesion.site")
                        page.goto(f"{self.live_server_url}/facts/{site_id}/", wait_until="networkidle")
                        page.get_by_role("button", name="撤销确认", exact=True).click()
                        page.goto(f"{self.live_server_url}/lesions/{lesion.pk}/", wait_until="networkidle")
                        expect(page.locator("main")).to_contain_text("来源或关联已变化，需重新核对")
                        expect(page.locator(".lesion-trend-segment")).to_have_count(0)
                        self.assertEqual(errors, [])
                        self.assertEqual(server_errors, [])
                    finally:
                        browser.close()
