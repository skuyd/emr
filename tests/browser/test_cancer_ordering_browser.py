from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.cancer_ordering.models import CandidateRevision, CollectionRun, DisplaySelection
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.documents.models import Document
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestCancerOrderingBrowser(SQLiteSerializedStaticLiveServerTestCase):
    @contextmanager
    def browser(self, client, width):
        from playwright.sync_api import sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': width, 'height': 844}, locale='zh-CN', timezone_id='Asia/Shanghai')
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key, 'url': self.live_server_url}])
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                page = context.new_page()
                errors, failed_assets = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: failed_assets.append(response.url)
                        if '/static/' in response.url and response.status >= 400 else None)
                yield page
                self.assertEqual(errors, [])
                self.assertEqual(failed_assets, [])
            finally:
                browser.close()

    def capture(self, page, name):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), page.viewport_size['width'])
        directory = os.environ.get('PHR_CANCER_BROWSER_ARTIFACT_DIR')
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / name), full_page=True)

    def flow(self, width):
        from apps.exports.pdf import _font, FONT
        from playwright.sync_api import expect
        from reportlab.pdfgen.canvas import Canvas

        client, patient = _patient(get_user_model(), 'cancer-browser-' + str(width))
        text = '病理诊断：右肺上叶浸润性腺癌 pT2aN1M0 ⅢA期。'
        document, _ = parsed_facts(patient, [text], polygons=False)
        _font()
        output = io.BytesIO()
        canvas = Canvas(output)
        canvas.setFont(FONT, 15)
        canvas.drawString(30, 720, '合成报告，仅供软件验收')
        canvas.drawString(30, 680, text)
        canvas.save()
        payload = output.getvalue()
        Document.objects.filter(pk=document.pk).update(sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload))
        store = InMemoryObjectStore()
        store.objects[document.original_object_key] = payload
        with patch('apps.documents.views.originals.get_object_store', return_value=store), self.browser(client, width) as page:
            page.goto(self.live_server_url + f'/cancer-ordering/?patient={patient.pk}', wait_until='networkidle')
            expect(page.get_by_role('heading', name='当前显示顺序：通用顺序', exact=True)).to_be_visible()
            self.assertFalse(_db(lambda: CollectionRun.objects.exists() or DisplaySelection.objects.exists()))
            page.get_by_role('button', name='重新收集当前报告表述', exact=True).click()
            expect(page.get_by_role('heading', name='当前显示顺序：通用顺序', exact=True)).to_be_visible()
            # Page-only evidence remains pending until the user opens the real
            # original and explicitly records their check.
            page.get_by_role('link', name='右肺上叶浸润性腺癌', exact=True).click()
            page.get_by_role('button', name='保存本次操作', exact=True).click()
            expect(page.get_by_role('alert')).to_contain_text('本次尚未保存')
            self.capture(page, f'error-{width}.png')
            checkbox = page.get_by_label('我已打开原件并核对这条表述及所属对象:', exact=True)
            described = checkbox.get_attribute('aria-describedby')
            self.assertIn('id_checked_original_error', described or '')
            self.assertFalse(_db(lambda: CandidateRevision.objects.exists()))
            page.get_by_role('link', name='打开原件并核对上下文', exact=True).click()
            expect(page.locator('[data-viewer-image]')).to_have_js_property('complete', True)
            self.capture(page, f'original-{width}.png')
            page.go_back(wait_until='networkidle')
            checkbox = page.get_by_label('我已打开原件并核对这条表述及所属对象:', exact=True)
            checkbox.focus()
            page.keyboard.press('Space')
            expect(checkbox).to_be_checked()
            page.get_by_role('button', name='保存本次操作', exact=True).focus()
            page.keyboard.press('Enter')
            expect(page.locator('main')).to_contain_text('已核对原件')
            self.capture(page, f'candidate-{width}.png')
            page.get_by_label('本次操作:', exact=True).select_option('DEFER')
            page.get_by_role('button', name='保存本次操作', exact=True).click()
            self.assertEqual(_db(lambda: resolve_ordering(patient)['profile']), 'GENERAL')
            page.get_by_label('本次操作:', exact=True).select_option('UNDO')
            page.get_by_role('button', name='保存本次操作', exact=True).click()
            self.assertEqual(_db(lambda: resolve_ordering(patient)['profile']), 'LUNG')
            page.get_by_role('link', name='癌种与指标顺序', exact=True).click()
            page.get_by_label('排列方式:', exact=True).select_option('MANUAL_PROFILE')
            page.get_by_label('手动显示顺序:', exact=True).select_option('PANCREAS')
            page.get_by_role('button', name='保存显示顺序', exact=True).focus()
            page.keyboard.press('Enter')
            expect(page.get_by_role('heading', name='当前显示顺序：胰腺癌指标顺序', exact=True)).to_be_visible()
            _db(lambda: parsed_facts(patient, ['出院诊断：胰腺癌。']))
            page.reload(wait_until='networkidle')
            expect(page.get_by_role('heading', name='当前显示顺序：胰腺癌指标顺序', exact=True)).to_be_visible()
            page.get_by_role('button', name='重新收集当前报告表述', exact=True).click()
            page.get_by_label('排列方式:', exact=True).select_option('AUTO')
            page.get_by_role('button', name='保存显示顺序', exact=True).click()
            expect(page.get_by_role('heading', name='当前显示顺序：通用顺序', exact=True)).to_be_visible()
            expect(page.get_by_text('报告表述存在不同癌种', exact=False)).to_be_visible()
            expect(page.get_by_role('link', name='胰腺癌', exact=True)).to_be_visible()
            self.capture(page, f'conflict-{width}.png')

    def test_phone_original_review_keyboard_history_and_explicit_preference(self):
        self.flow(360)

    def test_desktop_original_review_keyboard_history_and_explicit_preference(self):
        self.flow(1280)
