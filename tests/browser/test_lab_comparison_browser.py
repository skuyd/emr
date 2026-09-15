"""Synthetic acceptance of the comparison's single vertical reading surface."""

from tests.browser import test_advanced_trends_browser as advanced


class TestLabComparisonBrowser(advanced.TestAdvancedTrendsBrowser):
    # Existing multi-indicator coverage remains in its own suite.
    test_desktop_filters_independent_axes_and_opens_actual_source_image = None
    test_mobile_comparison_keyboard_scroll_and_explicit_patient_filter = None

    def test_reference_layout_colors_and_per_report_ranges(self):
        from playwright.sync_api import expect, sync_playwright

        client, patient, rows, _ = self._data('comparison-reference-layout')
        for index, row in enumerate(rows):
            row.reference_range_raw = '3-9' if index < 4 else '100-150' if index == 4 else '110-160'
            row.save(update_fields=['reference_range_raw'])
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client, 1280)
            page = context.new_page()
            page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
            wbc = page.locator('.comparison-indicator[data-indicator="LAB_WBC"]')
            hgb = page.locator('.comparison-indicator[data-indicator="LAB_HGB"]')
            expect(wbc.locator('.comparison-unit-column')).to_have_text('10^9/L')
            expect(wbc.locator('th .comparison-reference')).to_have_text('参考：3-9')
            expect(page.locator('.comparison-reference-column')).to_have_count(0)
            expect(page.get_by_role('heading', name='检验对比', exact=True)).to_have_count(1)
            expect(page.get_by_role('navigation', name='检验工作区').get_by_role('link', name='检验对比', exact=True)).to_have_count(0)
            normal = page.locator(f'#result-{rows[1].pk}').evaluate('(e) => getComputedStyle(e).color')
            for row in (rows[0], rows[3]):
                value = page.locator(f'#result-{row.pk}')
                self.assertNotEqual(value.evaluate('(e) => getComputedStyle(e).color'), normal)
                self.assertEqual(value.evaluate('(e) => getComputedStyle(e).color'),
                                 value.locator('..').locator('.comparison-abnormal').evaluate('(e) => getComputedStyle(e).color'))
            toggle = hgb.get_by_role('button', name='按报告查看')
            toggle.focus()
            page.keyboard.press('Enter')
            expect(hgb.get_by_text('报告参考：100-150', exact=True)).to_be_visible()
            expect(hgb.get_by_text('报告参考：110-160', exact=True)).to_be_visible()
            page.keyboard.press('Enter')
            expect(hgb.get_by_text('报告参考：100-150', exact=True)).to_be_hidden()
            for width in (1280, 360):
                page.set_viewport_size({'width': width, 'height': 800})
                scroll = page.locator('#comparison-results')
                scroll.evaluate('(e) => { e.scrollLeft = e.scrollWidth; }')
                page.wait_for_timeout(100)
                head = page.locator('.comparison-head-table th').first.bounding_box()
                body = hgb.locator('th').bounding_box()
                self.assertAlmostEqual(head['x'], body['x'], delta=1)
                self.assertAlmostEqual(head['width'], body['width'], delta=1)
                page.locator('.comparison-workspace').evaluate(
                    '(e) => window.scrollTo(0, window.scrollY + e.getBoundingClientRect().top - 120)')
                page.wait_for_timeout(100)
                expect(wbc.locator('th .comparison-reference')).to_be_in_viewport()
                self._capture(page, f'comparison-reference-{width}.png')
                self._assert_page_width(page)
            browser.close()

    def test_cell_review_color_is_limited_to_result_problems(self):
        from playwright.sync_api import expect, sync_playwright
        from unittest.mock import patch

        client, patient, rows, store = self._data('comparison-cell-review')
        rows[0].quality_issues = [{'code': 'date_conflict', 'fields': ['observation_date']}]
        rows[1].quality_issues = [{'code': 'recognition_uncertain', 'fields': ['raw_value']}]
        for row in rows[:2]:
            row.save(update_fields=['quality_issues'])
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store):
            browser, context = self._context(playwright, client, 360)
            page = context.new_page()
            page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
            date_value = page.locator(f'#result-{rows[0].pk}')
            expect(date_value.locator('..').locator('.comparison-abnormal')).to_have_count(0)
            review_value = page.locator(f'#result-{rows[1].pk}')
            expect(review_value.locator('..').locator('.comparison-abnormal')).to_have_count(0)
            self.assertNotIn('待核对', page.locator('.comparison-workspace').inner_text())
            expect(review_value).to_have_attribute('title', '结果依据需确认，点击查看原因')
            self.assertEqual(review_value.evaluate('(e) => getComputedStyle(e).textDecorationStyle'), 'dotted')
            self.assertNotEqual(review_value.evaluate('(e) => getComputedStyle(e).color'),
                                date_value.evaluate('(e) => getComputedStyle(e).color'))
            review_value.scroll_into_view_if_needed()
            self._capture(page, 'comparison-review-color.png')
            review_value.click()
            page.locator('summary').filter(has_text='数据质量提示').click()
            expect(page.get_by_text('识别不确定', exact=False)).to_be_visible()
            page.get_by_role('link', name='返回检验对比', exact=True).click()
            date_value.click()
            page.locator('summary').filter(has_text='数据质量提示').click()
            expect(page.get_by_text('日期冲突', exact=False)).to_be_visible()
            browser.close()

    def test_mobile_filters_trends_keyboard_and_result_return(self):
        from playwright.sync_api import expect, sync_playwright
        from unittest.mock import patch

        client, patient, rows, store = self._data('comparison-mobile')
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store):
            browser, context = self._context(playwright, client, 360)
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
            expect(page.get_by_role('heading', name='检验对比', exact=True)).to_be_visible()
            self._assert_page_width(page)
            expect(page.locator('.comparison-sparkline-cell').first).to_be_hidden()
            picker = page.locator('.comparison-category-picker')
            picker.locator('summary').focus()
            page.keyboard.press('Enter')
            expect(picker.get_by_label('血常规', exact=True)).to_be_visible()
            self.assertEqual(picker.get_by_label('血常规', exact=True).count(), 1)
            picker.get_by_label('血常规', exact=True).check()
            page.keyboard.press('Escape')
            expect(picker.locator('summary')).to_be_focused()
            page.get_by_role('button', name='筛选', exact=True).click()
            page.wait_for_load_state('networkidle')
            picker.locator('summary').click()
            expect(picker.get_by_label('血常规', exact=True)).to_be_checked()
            picker.get_by_role('button', name='清除选择', exact=True).click()
            expect(picker.get_by_label('血常规', exact=True)).not_to_be_checked()
            page.keyboard.press('Escape')
            page.get_by_label('显示趋势', exact=True).check()
            group = page.locator('[data-group-toggle]').first
            group.focus()
            page.keyboard.press('Enter')
            expect(group).to_have_attribute('aria-expanded', 'false')
            page.keyboard.press('Enter')
            expect(group).to_have_attribute('aria-expanded', 'true')
            scroll = page.locator('#comparison-results')
            scroll.focus()
            page.keyboard.press('ArrowRight')
            expect(scroll).not_to_have_js_property('scrollLeft', 0)
            self.assertEqual(scroll.evaluate('(e) => e.scrollHeight'), scroll.evaluate('(e) => e.clientHeight'))
            value = page.locator('.comparison-value').first
            value.scroll_into_view_if_needed()
            value.focus()
            value_id = value.get_attribute('id')
            value.click()
            page.wait_for_load_state('networkidle')
            self.assertIn(f'patient={patient.pk}', page.url)
            expect(page.get_by_role('heading', name='核对检验结果', exact=True)).to_be_visible()
            page.get_by_role('link', name='返回检验对比', exact=True).click()
            page.wait_for_load_state('networkidle')
            expect(page.locator('#' + value_id)).to_be_focused()
            expect(page.get_by_label('显示趋势', exact=True)).to_be_checked()
            page.reload(wait_until='networkidle')
            expect(page.get_by_label('显示趋势', exact=True)).to_be_checked()
            self._assert_page_width(page)
            self.assertEqual(errors, [])
            self._capture(page, 'comparison-mobile.png')
            browser.close()

    def test_header_alignment_desktop_and_zoom(self):
        from playwright.sync_api import expect, sync_playwright

        client, patient, _, _ = self._data('comparison-header')
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client, 1280)
            page = context.new_page()
            page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}', wait_until='networkidle')
            for width in (1280, 640, 360):
                page.set_viewport_size({'width': width, 'height': 800 if width != 640 else 400})
                page.locator('#comparison-results').evaluate('(e) => { e.scrollLeft = 180; }')
                page.locator('.comparison-indicator').last.scroll_into_view_if_needed()
                page.wait_for_timeout(100)
                head = page.locator('.comparison-head-table th').nth(1).bounding_box()
                body = page.locator('.comparison-indicator').last.locator('td').first.bounding_box()
                self.assertAlmostEqual(head['x'], body['x'], delta=1)
                self.assertAlmostEqual(head['width'], body['width'], delta=1)
                self._assert_page_width(page)
                hospital = page.locator('.comparison-institution').first
                hospital.focus()
                page.keyboard.press('Enter')
                expect(hospital).to_have_attribute('aria-expanded', 'true')
                page.keyboard.press('Enter')
            self._capture(page, 'comparison-header.png')
            browser.close()

    def test_multiselect_enter_history_and_long_institution(self):
        from datetime import date
        from apps.labs.dictionary import phase_two_dictionary
        from apps.processing.models import DocumentSummary
        from tests.labs.test_trends import _observation
        from playwright.sync_api import expect, sync_playwright

        client, patient, rows, _ = self._data('comparison-multiple-groups')
        alt = _observation(patient, date(2026, 8, 3), '20', code='LAB_ALT', standard_name='丙氨酸氨基转移酶', raw_unit='U/L')[1]
        alt.dictionary_version = phase_two_dictionary().version
        alt.save(update_fields=['dictionary_version'])
        name = '合成超长医院名称' * 18 + '完整名称结尾'
        DocumentSummary.objects.filter(parsing_version=rows[0].parsing_version).update(institution_raw=name)
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client, 360)
            page = context.new_page()
            page.goto(self.live_server_url + '/labs/compare/', wait_until='networkidle')
            picker = page.locator('.comparison-category-picker')
            picker.locator('summary').click()
            picker.get_by_label('血常规', exact=True).check()
            picker.get_by_label('肝功能', exact=True).check()
            page.keyboard.press('Escape')
            page.get_by_role('button', name='筛选', exact=True).click()
            page.wait_for_load_state('networkidle')
            self.assertEqual(page.locator('[data-group]').count(), 2)
            self.assertEqual(page.locator('.comparison-value').count(), 7)
            hospital = page.get_by_role('button', name=name, exact=True)
            self.assertLess(hospital.bounding_box()['height'], 60)
            hospital.click()
            expect(hospital).to_have_attribute('aria-expanded', 'true')
            self.assertLess(hospital.bounding_box()['height'], 210)
            expect(hospital).to_have_text(name)
            hospital.click()
            page.get_by_label('检验指标', exact=True).fill('lab_wbc')
            page.get_by_label('检验指标', exact=True).press('Enter')
            page.wait_for_load_state('networkidle')
            self.assertEqual(page.locator('.comparison-value').count(), 4)
            page.go_back(wait_until='networkidle')
            self.assertEqual(page.locator('.comparison-value').count(), 7)
            page.go_forward(wait_until='networkidle')
            self.assertEqual(page.locator('.comparison-value').count(), 4)
            picker.locator('summary').click()
            expect(picker.get_by_label('血常规', exact=True)).to_be_checked()
            expect(picker.get_by_label('肝功能', exact=True)).to_be_checked()
            self._assert_page_width(page)
            browser.close()
