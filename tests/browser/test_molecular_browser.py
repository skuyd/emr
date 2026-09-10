import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import override_settings

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_pathology_browser as pathology_browser_helpers
from tests.browser.test_phase_three_browser import _db


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestMolecularBrowser(SQLiteSerializedStaticLiveServerTestCase):
    original_store = pathology_browser_helpers.TestPathologyBrowser.original_store
    browser = pathology_browser_helpers.TestPathologyBrowser.browser

    def capture(self, page, name):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), page.viewport_size['width'])
        directory = os.environ.get('PHR_MOLECULAR_BROWSER_ARTIFACT_DIR')
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / name), full_page=True)

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
                        response = page.goto(self.live_server_url + f'/facts/{fields[key].pk}/', wait_until='networkidle')
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
                    page.goto(self.live_server_url + f'/facts/{fields["identity"].pk}/', wait_until='networkidle')
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
