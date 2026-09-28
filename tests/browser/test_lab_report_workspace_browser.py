from copy import copy
from unittest.mock import patch
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from playwright.sync_api import expect, sync_playwright

from apps.labs.report_workspace import report_workspace
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient, _pdf_bytes
from tests.labs.test_report_relations import report


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestLabReportWorkspaceBrowser(StaticLiveServerTestCase):
    def test_desktop_and_phone_edit_confirm_and_navigate(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        store = InMemoryObjectStore()
        cases = []
        for width in (1280, 360):
            client, patient = _patient(get_user_model(), f'report-workspace-browser-{width}')
            document, first, _unit = report(patient)
            second = copy(first)
            second.pk, second.reading_order, second.raw_name = uuid.uuid4(), first.reading_order + 1, '血红蛋白'
            second.save(force_insert=True)
            following, _, _ = report(patient, number=f'NEXT{width}', value='8')
            store.objects[document.original_object_key] = _pdf_bytes()
            store.objects[following.original_object_key] = _pdf_bytes()
            selected = next(item for item in report_workspace(patient)['reports'] if first.pk in {row.pk for row in item['rows']})
            cases.append((width, client, patient, first, second, selected))
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for width, client, patient, first, second, selected in cases:
                    context = browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 360)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                         'url': self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    response = page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}&report={selected["key"]}',
                                         wait_until='networkidle')
                    assert response.status == 200
                    expect(page.locator('[data-report-observation]')).to_have_count(2)
                    assert page.locator('[data-report-image]').evaluate('image => image.complete && image.naturalWidth > 0')
                    if width == 1280:
                        sampled_at = page.get_by_label('采样时间').first
                        original_time = sampled_at.input_value()
                        sampled_at.fill('2026-09-17')
                        page.get_by_role('button', name='保存修改').click()
                        expect(sampled_at).to_be_focused()
                        assert sampled_at.evaluate('(input) => !input.validity.valid')
                        sampled_at.fill(original_time)
                    if width == 360:
                        page.get_by_role('button', name='收起原图').click()
                        expect(page.locator('[data-report-image-content]')).to_be_hidden()
                        page.get_by_role('button', name='展开原图').click()
                    first_row = page.locator(f'[data-report-observation="{first.pk}"]')
                    second_row = page.locator(f'[data-report-observation="{second.pk}"]')
                    first_row.get_by_label('结果原文').fill('7')
                    second_row.get_by_label('结果原文').fill('9')
                    page.get_by_role('link', name='下一份').click()
                    expect(page.get_by_role('dialog', name='有未保存的修改')).to_be_visible()
                    page.get_by_role('button', name='继续编辑').click()
                    expect(first_row.get_by_label('结果原文')).to_have_value('7')
                    expect(second_row.get_by_label('结果原文')).to_have_value('9')
                    first_row.get_by_role('button', name='第 1 页').click()
                    expect(page.locator('[data-report-image-highlight]')).to_be_visible()
                    page.get_by_role('button', name='确认并下一份').click()
                    page.wait_for_url('**/labs/reports/review/?*', wait_until='networkidle')
                    expect(page.locator('[data-report-observation]')).to_have_count(1)
                    assert 'NEXT' in page.locator('header.labs-panel').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert errors == []
                    context.close()
            finally:
                browser.close()
