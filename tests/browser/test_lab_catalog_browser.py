"""Synthetic patient demographics and per-result reference UI acceptance."""

from unittest.mock import patch

from tests.browser import test_advanced_trends_browser as advanced


class TestLabCatalogBrowser(advanced.TestAdvancedTrendsBrowser):
    test_desktop_filters_independent_axes_and_opens_actual_source_image = None
    test_mobile_comparison_keyboard_scroll_and_explicit_patient_filter = None

    def test_demographics_and_result_phase_on_desktop_and_phone(self):
        from playwright.sync_api import expect, sync_playwright

        client, patient, rows, store = self._data('catalog-browser')
        row = rows[0]
        row.raw_name, row.standard_name = 'FSH', 'FSH'
        row.standard_code, row.raw_value, row.raw_unit = 'CANDIDATE_FSH', '6', 'IU/L'
        row.save()
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store):
            browser, context = self._context(playwright, client, 1280)
            page = context.new_page()
            page.goto(self.live_server_url + f'/me/?patient={patient.pk}', wait_until='networkidle')
            page.get_by_label('性别').select_option('F')
            page.get_by_label('出生日期').fill('2000-09-22')
            page.get_by_role('button', name='保存基本信息', exact=True).click()
            expect(page.get_by_text('患者基本信息已更新。', exact=True)).to_be_visible()
            page.goto(self.live_server_url + f'/labs/observations/{row.pk}/?patient={patient.pk}', wait_until='networkidle')
            page.get_by_label('阶段', exact=True).select_option('卵泡期')
            page.get_by_role('button', name='保存本次阶段', exact=True).click()
            expect(page.locator('.labs-panel').first).to_contain_text('标准参考范围：3.85–8.78 IU/L')
            for width in (1280, 360):
                page.set_viewport_size({'width': width, 'height': 800})
                page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
                indicator = page.locator('.comparison-indicator[data-indicator="LAB_CATALOG_030"]')
                expect(indicator).to_contain_text('促卵泡生成激素')
                expect(indicator).to_contain_text('卵泡期')
                expect(indicator).to_contain_text('标准参考范围：3.85–8.78 IU/L')
                self._assert_page_width(page)
                self._capture(page, f'catalog-reference-{width}.png')
            browser.close()


    def test_new_patient_requires_and_saves_demographics_on_desktop_and_phone(self):
        from datetime import date
        from apps.patients.models import Patient
        from playwright.sync_api import expect, sync_playwright

        client, patient, rows, store = self._data('catalog-new-patient')
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client, 1280)
            page = context.new_page()
            for width in (1280, 360):
                page.set_viewport_size({'width': width, 'height': 800})
                page.goto(self.live_server_url + '/patients/new/', wait_until='networkidle')
                expect(page.locator('#id_sex')).to_have_attribute('required', '')
                expect(page.locator('#id_birth_date')).to_have_attribute('required', '')
                page.locator('#id_display_name').fill(f'新建验收{width}')
                page.locator('#id_sex').select_option('F')
                page.locator('#id_birth_date').fill('2000-02-29')
                page.locator('[name="upload_authority"]').check()
                self._assert_page_width(page)
                page.get_by_role('button', name='创建档案', exact=True).click()
                page.wait_for_url(self.live_server_url + '/')
            browser.close()
        for width in (1280, 360):
            saved = Patient.objects.get(account_id=patient.account_id, display_name=f'新建验收{width}')
            self.assertEqual((saved.sex, saved.birth_date), ('F', date(2000, 2, 29)))
        patient.refresh_from_db()
        self.assertEqual((patient.sex, patient.birth_date), ('', None))
