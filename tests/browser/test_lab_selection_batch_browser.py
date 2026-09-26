"""End-to-end acceptance of explicit selection and complete-report confirmation."""

from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from playwright.sync_api import expect, sync_playwright

from tests.browser import test_advanced_trends_browser as advanced
from tests.labs.test_trends import _observation


class TestLabSelectionBatchBrowser(advanced.TestAdvancedTrendsBrowser):
    test_desktop_filters_independent_axes_and_opens_actual_source_image = None
    test_mobile_comparison_keyboard_scroll_and_explicit_patient_filter = None

    def _selection_flow(self, width):
        client, patient, rows, store = self._data(f'selection-{width}')
        _observation(patient, date(2026, 8, 3), '20', code='LAB_ALT', raw_name='ALT',
                     standard_name='丙氨酸氨基转移酶', raw_unit='U/L')
        _observation(patient, date(2026, 8, 3), '8', code='CANDIDATE_OTHER',
                     raw_name='目录外合成项目', standard_name='目录外合成项目')
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store):
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
                category = picker.get_by_label('血常规（急诊）', exact=True)
                expect(category).to_be_checked()
                category.uncheck()
                self.assertFalse(category.evaluate('(e) => e.indeterminate'))
                # Selection is staged until the existing filter button is submitted.
                expect(page.locator('.comparison-indicator')).to_have_count(4)
                category.check()
                detail = category.locator('xpath=ancestor::details[1]')
                if not detail.evaluate('(e) => e.open'):
                    detail.locator(':scope > summary').click()
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
                expect(page.get_by_role('heading', name='核对检验结果', exact=True)).to_be_visible()
                page.get_by_role('link', name='返回检验对比', exact=True).click()
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
                picker.get_by_label('血常规（急诊）', exact=True).check()
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

    def _batch_flow(self, width):
        from apps.labs.models import LabObservation, ObservationRevision
        from apps.labs.revisions import revise_observation
        from tests.documents.test_detail_viewer import _patient
        from tests.labs.test_report_relations import report

        client, patient = _patient(get_user_model(), f'batch-browser-{width}')
        _, first, _ = report(patient)
        _, duplicate, _ = report(patient)
        _, second, _ = report(patient, number='B200', value='7')
        _, outside, _ = report(patient, number='C300')
        error = LabObservation.objects.create(
            parsing_version=second.parsing_version, document_page=second.document_page,
            evidence=second.evidence, report_unit=second.report_unit, reading_order=100,
            raw_name='血红蛋白', standard_code='LAB_HGB', standard_name='血红蛋白',
            raw_value='120', raw_unit='g/L', result_type='NUMERIC', specimen='BLOOD',
            dictionary_version=second.dictionary_version, capability_level='STABLE',
        )
        revise_observation(patient.account, error.pk, action='REPORT_ERROR', changes={}, expected_revision=0)
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client, width)
            try:
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
                page.get_by_role('link', name='批量确认检验报告', exact=True).click()
                page.wait_for_load_state('networkidle')
                reports = page.locator('[data-batch-report]')
                expect(reports).to_have_count(3)
                a = reports.filter(has_text='A100')
                b = reports.filter(has_text='B200')
                c = reports.filter(has_text='C300')
                expect(a).to_contain_text('待确认 2')
                expect(b).to_contain_text('待确认 1')
                expect(b).to_contain_text('需跳过 1')
                b.locator('details > summary').click()
                expect(b).to_contain_text('识别有误')
                expect(b.get_by_role('link', name='处理此项', exact=True)).to_be_visible()
                expect(b.get_by_role('link', name='血红蛋白：120 g/L', exact=True)).to_be_visible()
                a.locator('input[type=checkbox]').check()
                b.locator('input[type=checkbox]').check()
                expect(c.locator('input[type=checkbox]')).not_to_be_checked()
                self._assert_page_width(page)
                self._capture(page, f'batch-preview-{width}.png')
                page.get_by_role('button', name='确认所选报告结果', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.get_by_role('status')).to_contain_text('确认 3')
                expect(page.get_by_role('status')).to_contain_text('跳过 1')
                expect(page.get_by_role('status')).to_contain_text('仍有 1 项尚未完成确认')
                expect(reports.filter(has_text='B200')).not_to_contain_text('全部已确认')
                expect(reports.filter(has_text='B200')).to_contain_text('识别有误')
                self._assert_page_width(page)
                self._capture(page, f'batch-result-{width}.png')
                self.assertEqual(errors, [])
            finally:
                browser.close()
        self.assertEqual(set(ObservationRevision.objects.filter(action='CONFIRM').values_list('observation_id', flat=True)),
                         {first.pk, duplicate.pk, second.pk})
        self.assertEqual(outside.revisions.count(), 0)
        self.assertEqual(error.revisions.count(), 1)

    def test_desktop_batch_confirms_complete_reports_and_shows_remaining(self):
        self._batch_flow(1280)

    def test_phone_batch_confirms_complete_reports_and_shows_remaining(self):
        self._batch_flow(360)
