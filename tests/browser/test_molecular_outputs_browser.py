"""Desktop and phone select a real unit, exchange a share, then observe expiry."""
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_pathology_browser as browser_helpers
from tests.browser import test_cloud_open_browser as tls_helpers
from tests.browser.test_phase_three_browser import _db
from tests.facts.molecular_factories import graph, variant_source
from tests.facts.pathology_factories import review
from tests.documents.test_detail_viewer import _patient


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
class TestMolecularOutputsBrowser(SQLiteSerializedStaticLiveServerTestCase):
    server_thread_class = tls_helpers.TLSLiveServerThread
    tls_url = tls_helpers.TestCloudOpenBrowser.tls_url
    original_store = browser_helpers.TestPathologyBrowser.original_store

    @contextmanager
    def browser(self, client, store):
        with patch('apps.documents.views.originals.get_object_store', return_value=store):
            with tls_helpers.TestCloudOpenBrowser.browser(self, client, 360) as (_, page, external, posts):
                requests = []
                page.on('request', lambda request: requests.append(request.all_headers()) if request.method == 'POST' else None)
                yield page
                self.assertEqual(external, [])
                self.assertEqual(posts, [])
                self.assertGreaterEqual(len(requests), 2)
                self.assertTrue(all(headers.get('origin') == self.tls_url for headers in requests))

    def exercise(self, width):
        from playwright.sync_api import expect
        client, patient, document, _, fields = graph(get_user_model(), 'molecular-browser-' + str(width))
        rows = [SimpleNamespace(text=text, polygon=[[.05,.05+i*.06],[.95,.05+i*.06],[.95,.10+i*.06],[.05,.10+i*.06]])
                for i,text in enumerate(['合成分子检测报告', '标本甲；检测甲', variant_source(), '01.20 %'])]
        store = self.original_store(document, rows=rows)
        for field in fields.values(): review(patient, field)
        viewer, _ = _patient(get_user_model(), 'molecular-browser-recipient-' + str(width))
        with self.browser(client, store) as page:
            page.set_viewport_size({'width': width, 'height': 900})
            self.assertEqual(page.goto(self.tls_url + '/visit/', wait_until='networkidle').status, 200)
            page.get_by_text('选择结构化报告与字段', exact=True).click()
            page.locator('[name="custom_clinical_fields"]').check()
            page.locator(f'[name="clinical_field_ids"][value="{fields["metric"].pk}"]').check()
            for checkbox in page.locator('[name="sections"]').all(): checkbox.uncheck()
            page.locator('[name="sections"][value="imaging"]').check()
            page.locator('[name="details"]').check()
            self.assertIn('完整有序原身份', page.locator('main').inner_text())
            page.get_by_role('button', name='预览内容与导出清单', exact=True).click()
            expect(page.get_by_role('heading', name='确认本次内容', exact=True)).to_be_visible()
            body = page.locator('main').inner_text()
            for value in ('01.20', 'NM_SYN.2', 'c.12+1G>A', 'codon 4', '选定标本', '选定检测'):
                self.assertIn(value, body)
            for value in ('标本甲', '检测甲', 'variant:a'): self.assertNotIn(value, body)
            self.capture_output(page, f'molecular-preview-{width}.png', width)
            self.assertEqual(page.goto(self.tls_url + f'/patients/{patient.pk}/shares/', wait_until='networkidle').status, 200)
            page.locator(f'[name="document_ids"][value="{document.pk}"]').check()
            page.locator(f'[name="clinical_field_ids"][value="{fields["metric"].pk}"]').check()
            for checkbox in page.locator('[name="sections"]').all(): checkbox.uncheck()
            page.locator('[name="sections"][value="imaging"]').check()
            page.get_by_role('button', name='生成分享链接', exact=True).click()
            expect(page.locator('#share-link')).to_be_visible()
            link = page.locator('#share-link').input_value()
            self.assertTrue(link.startswith(self.tls_url + '/'))
            context = page.context.browser.new_context(viewport={'width': width, 'height': 900}, locale='zh-CN', ignore_https_errors=True)
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': viewer.session.session_key, 'url': self.tls_url, 'secure': True}])
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.tls_url + '/') else route.abort())
            recipient = context.new_page()
            errors = []; recipient.on('pageerror', lambda error: errors.append(str(error)))
            try:
                recipient.goto(link, wait_until='domcontentloaded')
                expect(recipient.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
                expect(recipient.locator('[data-share-content]')).to_contain_text('NM_SYN.2')
                body = recipient.locator('main').inner_text()
                self.assertIn('01.20', body)
                for value in ('标本甲', '检测甲', 'variant:a'): self.assertNotIn(value, body)
                self.assertEqual(recipient.get_by_role('link', name='查看这份原件', exact=True).count(), 0)
                self.capture_output(recipient, f'molecular-share-{width}.png', width)
                _db(lambda: review(patient, fields['identity'], 'EXCLUDE'))
                self.assertEqual(recipient.reload(wait_until='domcontentloaded').status, 410)
                self.assertNotIn('NM_SYN.2', recipient.locator('body').inner_text())
                self.assertEqual(errors, [])
            finally:
                context.close()

    def capture_output(self, page, filename, width):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
        folder = os.environ.get('PHR_MOLECULAR_OUTPUT_BROWSER_ARTIFACT_DIR')
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(folder)/filename), full_page=True)

    def test_desktop_selected_output_and_fine_share(self): self.exercise(1280)

    def test_phone_selected_output_and_fine_share(self): self.exercise(360)
