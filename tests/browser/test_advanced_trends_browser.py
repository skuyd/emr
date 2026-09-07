from datetime import date
import io
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from pypdf import PdfWriter

from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestAdvancedTrendsBrowser(StaticLiveServerTestCase):
    def _capture(self, target, filename):
        directory = os.environ.get('PHR_TREND_BROWSER_ARTIFACT_DIR')
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            target.screenshot(path=str(folder / filename))

    def _assert_page_width(self, page):
        width = page.viewport_size['width']
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
        self.assertLessEqual(page.locator('.app-main').evaluate('(element) => element.scrollWidth'),
                             page.locator('.app-main').evaluate('(element) => element.clientWidth'))

    def _data(self, marker):
        client, patient = _patient(get_user_model(), marker)
        rows = [_observation(patient, date(2026, 8, day), value)[1]
                for day, value in ((1, '2'), (2, '4'), (3, '6'), (4, '12'))]
        for day, value in ((2, '120'), (4, '130')):
            rows.append(_observation(patient, date(2026, 8, day), value,
                                     code='LAB_HGB', standard_name='血红蛋白', raw_unit='g/L')[1])
        store = InMemoryObjectStore()
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        original = io.BytesIO()
        writer.write(original)
        for row in rows:
            document = row.parsing_version.document
            store.objects[document.original_object_key] = original.getvalue()
        return client, patient, rows, store

    def _context(self, playwright, client, width):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        context = browser.new_context(viewport={'width': width, 'height': 800}, locale='zh-CN')
        context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                             'url': self.live_server_url}])
        context.route('**/*', lambda route: route.continue_()
                      if route.request.url.startswith(self.live_server_url + '/') else route.abort())
        return browser, context

    def test_desktop_filters_independent_axes_and_opens_actual_source_image(self):
        from playwright.sync_api import expect, sync_playwright

        client, patient, rows, store = self._data('browser-advanced-desktop')
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store):
            browser, context = self._context(playwright, client, 1280)
            page = context.new_page()
            errors, asset_failures = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: asset_failures.append(response.status)
                    if '/static/' in response.url and response.status >= 400 else None)
            page.goto(self.live_server_url + f'/trends/?patient={patient.pk}', wait_until='networkidle')
            page.get_by_role('link', name='多指标对照', exact=True).click()
            page.get_by_label('白细胞计数', exact=True).check()
            page.get_by_label('血红蛋白', exact=True).check()
            page.get_by_label('开始日期', exact=True).fill('2026-08-01')
            page.get_by_label('结束日期', exact=True).fill('2026-08-04')
            page.get_by_role('button', name='更新对照', exact=True).click()
            page.wait_for_load_state('networkidle')
            self.assertEqual(page.locator('.trend-series').count(), 2)
            self.assertEqual(page.locator('.trend-value-axis').count(), 2)
            wbc = page.locator('.trend-series').filter(has=page.get_by_role('heading', name='白细胞计数 · 10^9/L'))
            hgb = page.locator('.trend-series').filter(has=page.get_by_role('heading', name='血红蛋白 · g/L'))
            self.assertEqual(wbc.locator('.trend-value-axis').inner_text().split(), ['12', '2'])
            self.assertEqual(hgb.locator('.trend-value-axis').inner_text().split(), ['130', '120'])
            # Both records on August 2 occupy the same actual-date x position.
            self.assertEqual(wbc.locator('circle').nth(1).get_attribute('cx'), hgb.locator('circle').first.get_attribute('cx'))
            change = wbc.locator('.personal-change').last
            change.locator('summary').focus()
            page.keyboard.press('Enter')
            self.assertTrue(change.evaluate('(element) => element.open'))
            self.assertIn('+200.00%', change.inner_text())
            self.assertIn('相隔 1 天', change.inner_text())
            self.assertEqual(change.locator('ul a').count(), 3)
            self._capture(wbc, 'desktop-series.png')
            page.get_by_label('开始日期', exact=True).fill('2026-08-03')
            page.get_by_role('button', name='更新对照', exact=True).click()
            page.wait_for_load_state('networkidle')
            self.assertEqual(page.locator('.trend-series').count(), 1)
            self.assertIn('血红蛋白：筛选范围内不足两个不同日期', page.locator('.trend-page').inner_text())
            change = page.locator('.personal-change').last
            change.locator('summary').click()
            self.assertIn('2026-08-01', change.locator('ul').inner_text())
            change.locator('ul a').first.click()
            original_image = page.locator('.labs-source img')
            expect(original_image).to_be_visible()
            expect(original_image).to_have_js_property('complete', True)
            self.assertGreater(original_image.evaluate('(element) => element.naturalWidth'), 0)
            self.assertIn(str(rows[0].pk), page.url)
            self.assertIn(patient.display_name, page.locator('.app-patient-context').inner_text())
            self.assertEqual(errors, [])
            self.assertEqual(asset_failures, [])
            browser.close()

    def test_mobile_comparison_keyboard_scroll_and_explicit_patient_filter(self):
        from playwright.sync_api import expect, sync_playwright

        client, patient, _, store = self._data('browser-advanced-mobile')
        self.assertEqual(client.post('/patients/new/', {'display_name': '另一位合成家人', 'upload_authority': 'on'}).status_code, 302)
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store):
            browser, context = self._context(playwright, client, 360)
            page = context.new_page()
            response = page.goto(self.live_server_url + reverse('labs:comparison') + f'?patient={patient.pk}', wait_until='networkidle')
            self.assertEqual(response.status, 200)
            self._assert_page_width(page)
            self._capture(page, 'mobile-comparison.png')
            group = page.locator('.comparison-group').first
            summary = group.locator(':scope > summary')
            summary.focus()
            page.keyboard.press('Space')
            self.assertFalse(group.evaluate('(element) => element.open'))
            page.keyboard.press('Enter')
            self.assertTrue(group.evaluate('(element) => element.open'))
            scroll = group.locator('.labs-table-scroll')
            self.assertGreater(scroll.evaluate('(element) => element.scrollWidth'), scroll.evaluate('(element) => element.clientWidth'))
            scroll.focus()
            page.keyboard.press('ArrowRight')
            expect(scroll).not_to_have_js_property('scrollLeft', 0)
            page.get_by_label('开始日期', exact=True).fill('2026-08-04')
            page.get_by_role('button', name='筛选', exact=True).click()
            page.wait_for_load_state('networkidle')
            self.assertIn(f'patient={patient.pk}', page.url)
            self.assertIn(patient.display_name, page.locator('.app-patient-context').inner_text())
            self._assert_page_width(page)
            page.get_by_role('link', name='打开多指标对照', exact=True).click()
            page.get_by_label('白细胞计数', exact=True).check()
            page.get_by_role('button', name='更新对照', exact=True).click()
            page.wait_for_load_state('networkidle')
            self._assert_page_width(page)
            self.assertIn('10^9/L', page.locator('.trend-series').inner_text())
            self._capture(page, 'mobile-joint.png')
            self._capture(page.locator('.trend-chart'), 'mobile-chart.png')
            browser.close()
