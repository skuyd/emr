from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from playwright.sync_api import expect, sync_playwright

from tests.browser.test_advanced_trends_browser import TestAdvancedTrendsBrowser as AdvancedTrendsBrowser
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation
from tests.labs.test_report_relations import report


class TestLabReportConsolidationBrowser(AdvancedTrendsBrowser):
    test_desktop_filters_independent_axes_and_opens_actual_source_image = None
    test_mobile_comparison_keyboard_scroll_and_explicit_patient_filter = None

    def test_conflicting_sources_remain_accessible_without_a_main_trend(self):
        client, patient = _patient(get_user_model(), 'report-conflict-browser')
        report(patient)
        _, _, unit = report(patient, at='2026-09-17 10:30')
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                for width in (1280, 360):
                    context = browser.new_context(viewport={'width': width, 'height': 800}, has_touch=width == 360)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                         'url': self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    response = page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
                    assert response.status == 200
                    expect(page.locator('.comparison-value')).to_have_count(2)
                    response = page.goto(self.live_server_url + f'/trends/LAB_WBC/?patient={patient.pk}', wait_until='networkidle')
                    assert response.status == 200
                    expect(page.locator('.trend-chart')).to_have_count(0)
                    summary = page.get_by_text('全部采样结果与来源', exact=True)
                    if width == 360:
                        summary.tap()
                    else:
                        summary.focus()
                        page.keyboard.press('Enter')
                    expect(page.get_by_text('报告归属存在冲突', exact=False).first).to_be_visible()
                    expect(page.get_by_role('link', name='查看原件依据')).to_have_count(2)
                    self._assert_page_width(page)
                    page.goto(self.live_server_url + f'/labs/reports/{unit.pk}/?patient={patient.pk}', wait_until='networkidle')
                    expect(page.get_by_text('报告归属存在冲突，请核对关联报告的原件。', exact=True)).to_be_visible()
                    assert errors == []
                    context.close()
            finally:
                browser.close()

    def test_keyboard_and_touch_can_expand_all_sampling_sources_and_reports(self):
        client, patient = _patient(get_user_model(), 'report-consolidation-browser')
        for value, clock, reference in (('5.0', '08:30', '1-10'), ('5.00', '10:30', '2-9'), ('6', '11:30', '1-10')):
            _, row = _observation(patient, date(2026, 9, 17), value, sampling_time=clock)
            row.reference_range_raw = reference
            row.save(update_fields=['reference_range_raw'])
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            for width in (1280, 360):
                context = browser.new_context(viewport={'width': width, 'height': 800}, has_touch=width == 360)
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                     'url': self.live_server_url}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                response = page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
                self.assertEqual(response.status, 200)
                expect(page.locator('.comparison-report-date')).to_have_count(1)
                expect(page.locator('.comparison-value')).to_have_count(2)
                expect(page.get_by_text('共 1 项指标 · 3 份报告 · 3 张原图 · 2 条展示结果')).to_be_visible()
                sources = page.locator('.comparison-sources').filter(has=page.locator('summary', has_text='2 个来源'))
                if width == 360:
                    sources.locator('summary').tap()
                else:
                    sources.locator('summary').focus()
                    page.keyboard.press('Enter')
                expect(sources).to_have_attribute('open', '')
                expect(sources).to_contain_text('2026-09-17 08:30')
                expect(sources).to_contain_text('2026-09-17 10:30')
                expect(sources).to_contain_text('1-10')
                expect(sources).to_contain_text('2-9')
                expect(page.get_by_text('参考信息有差异', exact=True)).to_be_visible()
                expect(sources.get_by_role('link', name='查看原图依据')).to_have_count(2)
                reports = page.locator('.comparison-report-date')
                reports.locator('summary').click()
                expect(reports.get_by_role('link')).to_have_count(3)
                self._assert_page_width(page)
                self._capture(page, f'lab-report-sources-{width}.png')
                self.assertEqual(errors, [])
                context.close()
            browser.close()
