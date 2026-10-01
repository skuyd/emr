from unittest.mock import patch

from django.urls import reverse

from tests.browser.lab_browser_helpers import LabBrowserTestCase


class TestLabNavigationBrowser(LabBrowserTestCase):
    def test_mobile_comparison_keyboard_scroll_and_explicit_patient_filter(self):
        from playwright.sync_api import expect, sync_playwright

        client, patient, _, store = self._data('browser-advanced-mobile')
        self.assertEqual(client.post('/patients/new/', {'display_name': '另一位合成家人', 'upload_authority': 'on', 'sex': 'F', 'birth_date': '2000-01-01'}).status_code, 302)
        with sync_playwright() as playwright, patch('apps.labs.views.get_object_store', return_value=store):
            browser, context = self._context(playwright, client, 360)
            page = context.new_page()
            response = page.goto(self.live_server_url + reverse('labs:comparison') + f'?patient={patient.pk}', wait_until='networkidle')
            self.assertEqual(response.status, 200)
            self._assert_page_width(page)
            self._capture(page, 'mobile-comparison.png')
            group = page.locator('.comparison-group').first
            summary = group.locator('[data-group-toggle]')
            summary.focus()
            page.keyboard.press('Space')
            expect(summary).to_have_attribute('aria-expanded', 'false')
            page.keyboard.press('Enter')
            expect(summary).to_have_attribute('aria-expanded', 'true')
            scroll = page.locator('.labs-table-scroll')
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
            browser.close()
