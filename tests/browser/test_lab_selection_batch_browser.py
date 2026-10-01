"""End-to-end acceptance of explicit comparison selection and report navigation."""

from datetime import date
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from tests.browser import lab_browser_helpers as advanced
from tests.labs.helpers import _observation


class TestLabSelectionBatchBrowser(advanced.LabBrowserTestCase):

    def _selection_flow(self, width):
        client, patient, rows, store = self._data(f'selection-{width}')
        _observation(patient, date(2026, 8, 3), '20', code='LAB_ALT', raw_name='ALT',
                     standard_name='丙氨酸氨基转移酶', raw_unit='U/L')
        _observation(patient, date(2026, 8, 3), '8', code='CANDIDATE_OTHER',
                     raw_name='目录外合成项目', standard_name='目录外合成项目')
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store), \
                patch('apps.documents.views.originals.get_object_store', return_value=store):
            browser, context = self._context(playwright, client, width)
            try:
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                response = page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}',
                                     wait_until='networkidle')
                self.assertEqual(response.status, 200)
                expect(page.locator('.comparison-indicator')).to_have_count(4)
                expect(page.locator('.comparison-group').last).to_contain_text('其他')
                picker = page.locator('.comparison-category-picker')
                picker.locator(':scope > summary').click()
                options = picker.locator('[data-indicator-option]')
                self.assertEqual(options.count(), 4)
                self.assertTrue(options.evaluate_all('(nodes) => nodes.every(n => n.checked)'))
                category = picker.get_by_label('血常规', exact=True)
                expect(category).to_be_checked()
                category.uncheck()
                self.assertFalse(category.evaluate('(e) => e.indeterminate'))
                # Selection is staged until the existing filter button is submitted.
                expect(page.locator('.comparison-indicator')).to_have_count(4)
                category.check()
                detail = category.locator('xpath=ancestor::details[1]')
                if not detail.evaluate('(e) => e.open'):
                    detail.locator(':scope > summary').click()
                expect(picker.get_by_label('白细胞', exact=True)).to_be_checked()
                picker.get_by_label('血红蛋白', exact=True).uncheck()
                expect(category).to_have_js_property('indeterminate', True)
                self._capture(page, f'selection-tree-{width}.png')
                picked = options.evaluate_all('(nodes) => nodes.filter(n => n.checked).map(n => n.value)')
                detail.locator(':scope > summary').click()
                self.assertEqual(options.evaluate_all('(nodes) => nodes.filter(n => n.checked).map(n => n.value)'), picked)
                page.keyboard.press('Escape')
                page.get_by_label('开始日期', exact=True).fill('2026-08-02')
                page.get_by_label('结束日期', exact=True).fill('2026-08-03')
                page.get_by_label('检验指标', exact=True).fill('lab_wbc')
                page.get_by_role('button', name='筛选', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.locator('.comparison-indicator')).to_have_count(1)
                expect(page.locator('.comparison-value')).to_have_count(2)
                expect(page.locator('.comparison-group')).to_have_count(1)
                group = page.locator('[data-group-toggle]')
                group.click()
                expect(page.locator('.comparison-indicator')).to_be_hidden()
                group.click()
                page.locator(f'#result-{rows[1].pk}').click()
                expect(page.get_by_role('heading', name='报告核对', exact=True)).to_be_visible()
                page.get_by_role('link', name='返回', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.get_by_label('开始日期', exact=True)).to_have_value('2026-08-02')
                expect(page.get_by_label('结束日期', exact=True)).to_have_value('2026-08-03')
                expect(page.get_by_label('检验指标', exact=True)).to_have_value('lab_wbc')
                picker.locator(':scope > summary').click()
                self.assertEqual(options.evaluate_all('(nodes) => nodes.filter(n => n.checked).map(n => n.value)'), picked)
                picker.get_by_role('button', name='清除选择', exact=True).click()
                page.keyboard.press('Escape')
                page.get_by_role('button', name='筛选', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.locator('.comparison-indicator')).to_have_count(0)
                expect(page.get_by_role('status')).to_contain_text('未选择检验指标')
                page.reload(wait_until='networkidle')
                expect(page.locator('.comparison-indicator')).to_have_count(0)
                picker.locator(':scope > summary').click()
                picker.get_by_label('血常规', exact=True).check()
                page.keyboard.press('Escape')
                page.get_by_role('button', name='筛选', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.locator('.comparison-indicator')).to_have_count(1)
                self._assert_page_width(page)
                self._capture(page, f'selection-{width}.png')
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_desktop_selection_filter_empty_and_return(self):
        self._selection_flow(1280)

    def test_phone_selection_filter_empty_and_return(self):
        self._selection_flow(360)
