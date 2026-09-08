from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.fakes import InMemoryObjectStore
from tests.facts.pathology_factories import ihc_fixture


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestPathologyBrowser(SQLiteSerializedStaticLiveServerTestCase):
    def original_store(self, document):
        from apps.documents.models import Document
        from apps.exports.pdf import FONT, _font
        from reportlab.pdfgen.canvas import Canvas

        _font()
        output = io.BytesIO()
        canvas = Canvas(output)
        canvas.setFont(FONT, 16)
        for i, line in enumerate(("合成病理与免疫组化报告，仅用于软件测试", "标本甲；检测甲；PD-L1；SYN-CLONE-A", "TPS 13%；CPS 21（未印刷单位）")):
            canvas.drawString(42, 730 - i * 40, line)
        canvas.save()
        payload = output.getvalue()
        document.sha256, document.byte_size = hashlib.sha256(payload).hexdigest(), len(payload)
        Document.objects.filter(pk=document.pk).update(sha256=document.sha256, byte_size=document.byte_size)
        store = InMemoryObjectStore()
        staged = store.put_staging(io.BytesIO(payload), expected_size=len(payload), expected_sha256=document.sha256)
        store.promote_immutable(staged, document.original_object_key)
        return store

    @contextmanager
    def browser(self, client, store):
        from playwright.sync_api import sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        with patch("apps.documents.views.originals.get_object_store", return_value=store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={"width": 360, "height": 844}, locale="zh-CN", timezone_id="Asia/Shanghai")
                context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": client.session.session_key, "url": self.live_server_url}])
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                page = context.new_page()
                errors, failed_assets = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("response", lambda response: failed_assets.append(response.url) if "/static/" in response.url and response.status >= 400 else None)
                yield page
                self.assertEqual(errors, [])
                self.assertEqual(failed_assets, [])
            finally:
                browser.close()

    def capture(self, page, name):
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 360)
        directory = os.environ.get("PHR_PATHOLOGY_BROWSER_ARTIFACT_DIR")
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / name), full_page=True)

    def test_phone_original_actual_graph_confirm_and_group_undo_after_other_patient_tab(self):
        from apps.facts.models import Fact
        from apps.facts.readmodels import effective_fact
        from apps.patients.services import create_patient_space
        from tests.documents.test_detail_viewer import CONFIRMATIONS, EVIDENCE
        from playwright.sync_api import expect

        client, patient, document, report, fields = ihc_fixture(get_user_model(), "pathology-phone-graph")
        other = create_patient_space(patient.account, "合成另一患者", CONFIRMATIONS, EVIDENCE)
        store = self.original_store(document)
        with self.browser(client, store) as page:
            for key in ("specimen", "assay", "clone", "marker", "tps", "cps"):
                response = page.goto(self.live_server_url + f"/facts/{fields[key].pk}/", wait_until="networkidle")
                self.assertEqual(response.status, 200, key + ": " + page.locator("body").inner_text())
                if key == "cps":
                    expect(page.frame_locator("iframe").locator("[data-viewer-image]")).to_have_js_property("complete", True)
                    self.assertGreater(page.frame_locator("iframe").locator("[data-viewer-image]").evaluate("image => image.naturalWidth"), 0)
                    expect(page.locator("#id_score_kind")).to_have_value("CPS")
                    expect(page.locator("#id_original_unit")).to_have_value("")
                    second = page.context.new_page()
                    second.goto(self.live_server_url + f"/?patient={other.pk}", wait_until="networkidle")
                    second.close()
                checkbox = page.get_by_label("我已对照原件核对字段、标本、检测和原文限定:", exact=True)
                self.assertEqual(checkbox.count(), 1, key + ": " + page.locator("main").inner_text())
                checkbox.check()
                page.get_by_role("button", name="确认原文字段", exact=True).click()
                expect(page.get_by_role("heading", name="已核对", exact=True)).to_be_visible()
            expect(page.frame_locator("iframe").locator("[data-viewer-image]")).to_have_js_property("complete", True)
            self.assertGreater(page.frame_locator("iframe").locator("[data-viewer-image]").evaluate("image => image.naturalWidth"), 0)
            self.capture(page, "cps-reviewed-phone.png")
            current = _db(lambda: effective_fact(Fact.objects.get(pk=fields["cps"].pk)))
            self.assertTrue(current["usable"])
            self.assertIsNone(current["content"]["value"]["unit"])
            page.goto(self.live_server_url + f"/facts/{fields['marker'].pk}/", wait_until="networkidle")
            page.get_by_role("link", name="整体更正标本或检测关联", exact=True).click()
            expect(page.get_by_role("heading", name="整组关联替换", exact=True)).to_be_visible()
            self.assertEqual(page.locator("fieldset").count(), 3)
            page.get_by_label("我已逐项核对整组的原件文字、标本和检测关联:", exact=True).check()
            page.get_by_role("button", name="保存整组替代字段", exact=True).click()
            expect(page.get_by_role("heading", name="已排除", exact=True)).to_be_visible()
            self.assertEqual(page.get_by_role("link", name="查看替代字段并重新核对", exact=True).count(), 3)
            expect(page.frame_locator("iframe").locator("[data-viewer-image]")).to_have_js_property("complete", True)
            self.assertGreater(page.frame_locator("iframe").locator("[data-viewer-image]").evaluate("image => image.naturalWidth"), 0)
            self.capture(page, "context-replaced-phone.png")
            page.get_by_role("button", name="撤销整组关联替换", exact=True).click()
            expect(page.get_by_role("heading", name="待核对", exact=True)).to_be_visible()
            self.assertFalse(_db(lambda: effective_fact(Fact.objects.get(pk=fields["cps"].pk)))["usable"])

    def test_phone_manual_node_groups_keep_separate_missing_counts(self):
        from apps.facts.models import Fact
        from playwright.sync_api import expect

        client, patient, document, report, fields = ihc_fixture(get_user_model(), "pathology-phone-nodes")
        store = self.original_store(document)
        with self.browser(client, store) as page:
            page.goto(self.live_server_url + f"/facts/reports/{report.pk}/?field_key=specimen.nodes", wait_until="networkidle")
            entry = page.locator("#manual-field form[method=post]")
            entry.get_by_role("button", name="新增一组原文计数", exact=True).focus()
            page.keyboard.press("Enter")
            expect(entry.locator('input[name="node_count"]')).to_have_value("2")
            expect(entry.locator('input[name="group_2_label"]')).to_be_focused()
            entry.locator('textarea[name="raw_value"]').fill("合成组甲送检5，组乙阳性2；标本甲")
            for key, value in {"group_1_label": "合成组甲", "group_1_sampled": "5", "group_1_raw": "合成组甲送检5",
                               "group_2_label": "合成组乙", "group_2_positive": "2", "group_2_raw": "合成组乙阳性2"}.items():
                entry.locator(f'[name="{key}"]').fill(value)
            entry.locator('[name="binding_specimen"]').select_option(str(fields["specimen"].pk))
            entry.locator('[name="proof_specimen"]').fill("标本甲")
            entry.locator('[name="proof_page_specimen"]').fill("1")
            self.capture(page, "manual-node-groups-phone.png")
            entry.get_by_role("button", name="保存补录并核对", exact=True).click()
            expect(page.get_by_role("heading", name="核对病理淋巴结原文计数", exact=True)).to_be_visible()
            value = _db(lambda: Fact.objects.get(clinical_report=report, field_key="specimen.nodes").automatic_content["value"])
            self.assertEqual([(g["sampled"], g["positive"]) for g in value["groups"]], [("5", None), (None, "2")])
