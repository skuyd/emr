import os
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_pathology_browser as pathology_browser_helpers
from tests.browser.test_cloud_open_browser import TLSLiveServerThread
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
class TestMolecularBrowser(SQLiteSerializedStaticLiveServerTestCase):
    server_thread_class = TLSLiveServerThread
    original_store = pathology_browser_helpers.TestPathologyBrowser.original_store

    @property
    def tls_url(self):
        return self.live_server_url.replace('http:', 'https:', 1)

    @contextmanager
    def browser(self, client, store):
        from playwright.sync_api import sync_playwright
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        with patch('apps.documents.views.originals.get_object_store', return_value=store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': 360, 'height': 844}, locale='zh-CN',
                    timezone_id='Asia/Shanghai', ignore_https_errors=True)
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key, 'url': self.tls_url, 'secure': True}])
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.tls_url + '/') else route.abort())
                page = context.new_page()
                errors, failed_assets = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: failed_assets.append(response.url) if '/static/' in response.url and response.status >= 400 else None)
                yield page
                self.assertEqual(errors, [])
                self.assertEqual(failed_assets, [])
            finally:
                browser.close()

    def capture(self, page, name):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), page.viewport_size['width'])
        directory = os.environ.get('PHR_MOLECULAR_BROWSER_ARTIFACT_DIR')
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / name), full_page=True)

    def test_phone_actual_automatic_source_images_and_highlights_preserve_components(self):
        from django.test import Client
        from playwright.sync_api import expect
        from tests.facts.test_molecular_pipeline import fixture, report_rows
        from apps.facts.models import Fact
        from apps.facts.readmodels import effective_fact
        rows = report_rows()
        patient, document, _, _ = fixture(get_user_model(), rows, name='molecular-auto-browser')
        client = Client()
        client.force_login(patient.account)
        store = self.original_store(document, rows=rows)
        report = document.clinical_reports.get()
        fields = {key: report.fields.get(field_key=key) for key in ('variant.identity', 'assay.tmb_value', 'assay.msi_category')}
        with self.browser(client, store) as page:
            for key, fact in fields.items():
                response = page.goto(self.tls_url + f'/facts/{fact.pk}/', wait_until='networkidle')
                self.assertEqual(response.status, 200)
                source = page.frame_locator('iframe')
                expect(source.locator('[data-viewer-image]')).to_have_js_property('complete', True)
                self.assertGreater(source.locator('[data-viewer-image]').evaluate('image => image.naturalWidth'), 0)
                expect(source.locator('[data-viewer-highlight]')).to_be_visible()
                self.assertGreater(source.locator('[data-viewer-highlight]').bounding_box()['width'], 0)
                if key == 'variant.identity':
                    expect(page.locator('#id_transcripts_raw')).to_have_value('NM_SYN.2')
                    expect(page.locator('#id_locations_raw')).to_have_value('build-X chr2:12')
                elif key == 'assay.tmb_value':
                    expect(page.locator('#id_scalar_1')).to_have_value('08.50')
                    expect(page.locator('#id_comparator')).to_have_value('GE')
                    expect(page.locator('#id_unit')).to_have_value('mut/Mb')
                else:
                    expect(page.locator('#id_code')).to_have_value('MSI_L')
                self.capture(page, key.replace('.', '-') + '-source-360.png')
                page.get_by_label('我已逐项对照原件核对原值、范围和必要归属:', exact=True).check()
                page.get_by_role('button', name='确认原文字段', exact=True).click()
                expect(page.get_by_role('heading', name='原文已核对，关联待核对', exact=True)).to_be_visible()
                self.assertFalse(_db(lambda: effective_fact(Fact.objects.get(pk=fact.pk))['usable']))

    def test_desktop_and_phone_actual_original_review_and_group_undo(self):
        from playwright.sync_api import expect
        from apps.facts.models import Fact
        from apps.facts.readmodels import effective_fact
        from tests.facts.molecular_factories import graph
        from tests.facts.test_clinical_segments import block
        rows = [block('合成分子报告；标本甲；检测甲', order=0),
                block('SYN1 c.12+1G>A (p.?)；codon 4；NM_SYN.2；build-X chr2:12；01.20 %', order=1)]
        for width in (360, 1280):
            with self.subTest(width=width):
                client, patient, document, report, fields = graph(get_user_model(), f'molecular-browser-{width}')
                store = self.original_store(document, rows=rows)
                with self.browser(client, store) as page:
                    page.set_viewport_size({'width': width, 'height': 844})
                    for key in ('specimen', 'assay', 'identity', 'metric'):
                        response = page.goto(self.tls_url + f'/facts/{fields[key].pk}/', wait_until='networkidle')
                        self.assertEqual(response.status, 200)
                        if key == 'identity':
                            expect(page.locator('#id_transcripts_raw')).to_have_value('NM_SYN.2')
                            expect(page.locator('#id_locations_raw')).to_have_value('build-X chr2:12')
                        label = ('我已逐项对照原件核对原值、范围和必要归属:' if fields[key].schema_version == 'MOLECULAR_REPORT_V1'
                                 else '我已对照原件核对字段、标本、检测和原文限定:')
                        page.get_by_label(label, exact=True).check()
                        page.get_by_role('button', name='确认原文字段', exact=True).click()
                        expect(page.get_by_role('heading', name='已核对', exact=True)).to_be_visible()
                    source = page.frame_locator('iframe').locator('[data-viewer-image]')
                    expect(source).to_have_js_property('complete', True)
                    self.assertGreater(source.evaluate('image => image.naturalWidth'), 0)
                    expect(page.locator('#id_scalar_1')).to_have_value('01.20')
                    expect(page.locator('#id_unit')).to_have_value('%')
                    self.capture(page, f'molecular-confirmed-{width}.png')
                    self.assertTrue(_db(lambda: effective_fact(Fact.objects.get(pk=fields['metric'].pk))['usable']))
                    page.goto(self.tls_url + f'/facts/{fields["identity"].pk}/', wait_until='networkidle')
                    page.get_by_role('link', name='整体更正标本或检测关联', exact=True).click()
                    expect(page.get_by_role('heading', name='整组关联替换', exact=True)).to_be_visible()
                    self.assertEqual(page.locator('fieldset').count(), 2)
                    page.get_by_label('我已逐项核对整组的原件文字、标本和检测关联:', exact=True).check()
                    page.get_by_role('button', name='保存整组替代字段', exact=True).click()
                    expect(page.get_by_role('heading', name='已排除', exact=True)).to_be_visible()
                    self.assertEqual(page.get_by_role('link', name='查看替代字段并重新核对', exact=True).count(), 2)
                    page.get_by_role('button', name='撤销整组关联替换', exact=True).click()
                    expect(page.get_by_role('heading', name='待核对', exact=True)).to_be_visible()
                    self.assertFalse(_db(lambda: effective_fact(Fact.objects.get(pk=fields['metric'].pk))['usable']))
                    source = page.frame_locator('iframe').locator('[data-viewer-image]')
                    expect(source).to_have_js_property('complete', True)
                    self.assertGreater(source.evaluate('image => image.naturalWidth'), 0)
                    self.capture(page, f'molecular-group-undo-{width}.png')
