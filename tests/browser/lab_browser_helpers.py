from datetime import date
import io
import os
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from pypdf import PdfWriter

from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.labs.helpers import _observation


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class LabBrowserTestCase(StaticLiveServerTestCase):
    def _capture(self, target, filename):
        directory = os.environ.get('PHR_LAB_BROWSER_ARTIFACT_DIR')
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
                                     code='LAB_HGB', raw_name='血红蛋白', standard_name='血红蛋白', raw_unit='g/L')[1])
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
