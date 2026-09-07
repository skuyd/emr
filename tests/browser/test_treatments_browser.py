from datetime import date
import io
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from pypdf import PdfWriter

from apps.treatments.models import CycleLineage, CycleRecordLink, TreatmentCycle, TreatmentEvent
from tests.browser.sqlite_server import SQLiteSerializedLiveServerThread
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _document, _patient
from tests.labs.test_trends import _observation
from tests.treatments.test_cycle_decisions import cycle
from tests.treatments.test_derivation_service import fact
from tests.treatments.test_manual_events import create


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestTreatmentsBrowser(StaticLiveServerTestCase):
    server_thread_class = SQLiteSerializedLiveServerThread

    def _context(self, playwright, client, width=1280):
        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser")
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        context = browser.new_context(viewport={"width": width, "height": 850}, locale="zh-CN")
        context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": client.session.session_key, "url": self.live_server_url}])
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
        return browser, context

    def _capture(self, page, name):
        directory = os.environ.get("PHR_TREATMENT_BROWSER_ARTIFACT_DIR")
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / (name + ".png")), full_page=True)

    def _store(self, documents):
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        payload = io.BytesIO()
        writer.write(payload)
        store = InMemoryObjectStore()
        for document in documents:
            store.objects[document.original_object_key] = payload.getvalue()
        return store

    def test_desktop_automatic_confirmation_full_points_and_actual_source_image(self):
        from playwright.sync_api import expect, sync_playwright
        client, patient = _patient(get_user_model(), "treatment-browser-auto")
        origin = fact(patient, "2024-02-29给予方案甲C1D1化疗。2024-03-21给予方案甲C2D1化疗。")
        readings = [_observation(patient, date(2024, 3, day), value, code="LAB_NEUT_COUNT", raw_name="NEU#", standard_name="中性粒细胞计数")
                    for day, value in [(1, "3"), (2, "2"), (3, "1"), (4, "2.5")]]
        store = self._store([origin.document, *[item[0] for item in readings]])
        with sync_playwright() as playwright, patch("apps.labs.views.get_object_store", return_value=store), patch("apps.documents.views.originals.get_object_store", return_value=store):
            browser, context = self._context(playwright, client)
            page = context.new_page()
            failures, errors = [], []
            page.on("response", lambda response: failures.append(response.url) if response.status >= 500 or ("/static/" in response.url and response.status >= 400) else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(self.live_server_url + f"/treatments/?patient={patient.pk}", wait_until="networkidle")
                proposal = page.locator(".treatment-proposal").filter(has=page.get_by_role("heading", name="锚点 2024-02-29 · 原文 C1"))
                proposal.get_by_label("我已核对提议所列的原件或本人记录", exact=True).check()
                proposal.get_by_role("button", name="核对并确认此提议", exact=True).click()
                page.wait_for_load_state("networkidle")
                expect(page.get_by_role("heading", name="自动提议原稿", exact=True)).to_be_visible()
                cycle_path = page.url
                self.assertIn("已确认", page.locator(".treatment-page").inner_text())
                page.get_by_role("link", name="治疗与周期", exact=True).click()
                page.wait_for_load_state("networkidle")
                key_count = page.locator(".treatment-chart circle").count()
                self.assertEqual(key_count, 2)
                page.get_by_label("图表模式", exact=True).select_option("full")
                page.get_by_role("button", name="更新显示", exact=True).click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(page.locator(".treatment-chart circle").count(), 4)
                self._capture(page, "desktop-actual-cycles")
                source = page.locator(".treatment-chart a").first
                source.focus()
                page.keyboard.press("Enter")
                image = page.locator(".labs-source img")
                expect(image).to_be_visible()
                expect(image).to_have_js_property("complete", True)
                self.assertGreater(image.evaluate("element => element.naturalWidth"), 0)
                page.goto(cycle_path, wait_until="networkidle")
                page.get_by_role("button", name="撤销确认", exact=True).click()
                page.wait_for_load_state("networkidle")
                self.assertIn("尚待确认", page.locator(".treatment-page").inner_text())
                self.assertEqual(failures, [])
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_manual_events_cycle_assignment_split_merge_and_reject_are_complete_forms(self):
        from playwright.sync_api import expect, sync_playwright
        client, patient = _patient(get_user_model(), "treatment-browser-decisions")
        document, _ = _document(patient)
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client)
            page = context.new_page()
            try:
                events = []
                for title, day in [("本人事件甲", "2024-02-29"), ("本人事件乙", "2024-03-21")]:
                    page.goto(self.live_server_url + "/treatments/events/new/", wait_until="networkidle")
                    page.get_by_label("事件名称", exact=True).fill(title)
                    page.get_by_label("事件日期", exact=True).fill(day)
                    page.get_by_label("日期精度", exact=True).select_option("DAY")
                    page.get_by_role("button", name="保存本人补记", exact=True).click()
                    page.wait_for_load_state("networkidle")
                    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                    events.append(page.url.rstrip("/").split("/")[-1])
                page.goto(self.live_server_url + "/treatments/cycles/new/", wait_until="networkidle")
                page.locator(f'input[name="event_ids"][value="{events[0]}"]').check()
                page.locator(f'input[name="event_ids"][value="{events[1]}"]').check()
                page.get_by_label("我已核对原件或本人记录", exact=True).check()
                page.get_by_role("button", name="保存周期", exact=True).click()
                page.wait_for_load_state("networkidle")
                expect(page.get_by_role("heading", name="周期核对", exact=True)).to_be_visible()
                page.get_by_role("link", name="更正检查归属", exact=True).click()
                page.wait_for_load_state("networkidle")
                page.get_by_label("我已核对原件或本人记录", exact=True).check()
                page.get_by_role("button", name="保存归属决定", exact=True).click()
                page.wait_for_load_state("networkidle")
                page.get_by_role("link", name="拆分此周期", exact=True).click()
                page.wait_for_load_state("networkidle")
                self.assertEqual(page.locator('input[name="parts-TOTAL_FORMS"]').input_value(), "2")
                for index, identity in enumerate(events):
                    page.locator(f'input[name="parts-{index}-event_ids"][value="{identity}"]').check()
                page.get_by_label("我已核对原件或本人记录", exact=True).check()
                page.get_by_role("button", name="核对并拆分", exact=True).click()
                page.wait_for_load_state("networkidle")
                expect(page.get_by_role("heading", name="合并与拆分关系", exact=True)).to_be_visible()
                page.get_by_role("link", name="合并周期", exact=True).click()
                page.wait_for_load_state("networkidle")
                for control in page.locator('input[name="cycle_ids"]').all():
                    control.check()
                page.get_by_label("我已核对原件或本人记录", exact=True).check()
                page.get_by_role("button", name="核对并合并", exact=True).click()
                page.wait_for_load_state("networkidle")
                expect(page.get_by_role("heading", name="周期核对", exact=True)).to_be_visible()
                page.get_by_label("周期说明", exact=True).fill("已核对两个原事件，保留未知锚点")
                page.get_by_label("我已核对原件或本人记录", exact=True).check()
                page.get_by_role("button", name="保存更正并确认", exact=True).click()
                page.wait_for_load_state("networkidle")
                page.get_by_role("button", name="拒绝此提议", exact=True).click()
                page.wait_for_load_state("networkidle")
                self.assertIn("已拒绝", page.locator(".treatment-page").inner_text())
                self._capture(page, "desktop-all-decisions")
            finally:
                browser.close()
        self.assertEqual(TreatmentEvent.objects.filter(patient=patient).count(), 2)
        self.assertEqual(TreatmentCycle.objects.filter(patient=patient).count(), 4)
        self.assertEqual(CycleLineage.objects.count(), 4)
        self.assertTrue(CycleRecordLink.objects.filter(document=document, active=True, assigned=False).exists())

    def test_phone_calendar_cycle_keyboard_chart_and_old_tab_patient_scope(self):
        from playwright.sync_api import expect, sync_playwright
        client, patient = _patient(get_user_model(), "treatment-browser-phone")
        cycle(patient, [create(patient, patient.account)])
        for day, value in [(1, "3"), (2, "1"), (3, "2")]:
            _observation(patient, date(2024, 3, day), value, code="LAB_NEUT_COUNT", raw_name="NEU#", standard_name="中性粒细胞计数")
        self.assertEqual(client.post("/patients/new/", {"display_name": "另一位合成家人", "upload_authority": "on"}).status_code, 302)
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client, 360)
            page = context.new_page()
            try:
                page.goto(self.live_server_url + f"/treatments/?patient={patient.pk}", wait_until="networkidle")
                for layout in ["calendar", "cycles"]:
                    page.get_by_label("资料组织方式", exact=True).select_option(layout)
                    page.get_by_role("button", name="更新显示", exact=True).click()
                    page.wait_for_load_state("networkidle")
                    self.assertIn(f"patient={patient.pk}", page.url)
                    self.assertIn(patient.display_name, page.locator(".app-patient-context").inner_text())
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 360)
                chart = page.locator(".treatment-chart").first
                self.assertGreater(chart.evaluate("element => element.scrollWidth"), chart.evaluate("element => element.clientWidth"))
                chart.focus()
                page.keyboard.press("ArrowRight")
                expect(chart).not_to_have_js_property("scrollLeft", 0)
                details = page.locator("details").filter(has=page.locator(".treatment-table"))
                details.locator("summary").focus()
                page.keyboard.press("Enter")
                self.assertTrue(details.evaluate("element => element.open"))
                self.assertEqual(details.locator("tbody tr").count(), 3)
                self._capture(page, "phone-cycle-table")
            finally:
                browser.close()
