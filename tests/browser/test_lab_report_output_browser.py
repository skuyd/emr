from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from playwright.sync_api import expect, sync_playwright

from apps.patients.sharing import create_share
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient, _pdf_bytes
from tests.labs.test_report_relations import report
from tests.patients.test_family_shares import exchange


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestLabReportOutputBrowser(SQLiteSerializedStaticLiveServerTestCase):
    def test_shared_folded_results_show_all_sources_on_desktop_and_touch_screen(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        _, patient = _patient(get_user_model(), 'lab-output-browser-owner')
        viewer, _ = _patient(get_user_model(), 'lab-output-browser-viewer')
        documents, store = [], InMemoryObjectStore()
        for number, time, value in (('A1', '08:30', '5.0'), ('A2', '10:30', '5.00'), ('A3', '11:30', '6')):
            document, _, _ = report(patient, number=number, at='2026-09-17 ' + time, value=value)
            store.objects[document.original_object_key] = _pdf_bytes()
            documents.append(document)
        created = create_share(patient, patient.account, {'document_ids': [str(document.pk) for document in documents],
            'sections': ['labs', 'sources']})
        share_id = exchange(viewer, created.token)
        session = viewer.session.session_key
        with patch('apps.patients.share_views.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for width in (1280, 360):
                    context = browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 360)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': session, 'url': self.live_server_url}])
                    page, errors = context.new_page(), []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    response = page.goto(self.live_server_url + f'/shared/{share_id}/', wait_until='domcontentloaded')
                    assert response.status == 200
                    column = page.locator('.shared-lab-column')
                    expect(column).to_have_count(1)
                    expect(column.locator('article')).to_have_count(2)
                    expect(column.locator('li')).to_have_count(3)
                    for time in ('08:30', '10:30', '11:30'):
                        expect(column).to_contain_text('2026-09-17 ' + time)
                    expect(column).to_contain_text('3 份报告 · 3 张原图 · 2 条展示结果')
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    source = column.get_by_role('link', name='查看原件').first
                    with page.expect_response(lambda response: '/pages/1/image/' in response.url) as image_response:
                        if width == 360:
                            source.tap()
                        else:
                            source.focus()
                            page.keyboard.press('Enter')
                    assert image_response.value.status == 200
                    page.wait_for_url('**/documents/*/')
                    page.wait_for_function('() => Array.from(document.querySelectorAll("figure img")).some(image => image.complete && image.naturalWidth > 0)')
                    assert errors == []
                    context.close()
            finally:
                browser.close()
