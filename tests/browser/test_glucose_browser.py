from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from apps.glucose.models import GlucoseRecord
from apps.glucose.services import create_record
from tests.browser.sqlite_server import SQLiteSerializedLiveServerThread
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient, _pdf_bytes
from tests.glucose.factories import lab_source
from tests.glucose.test_forms import values


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestGlucoseBrowser(StaticLiveServerTestCase):
    server_thread_class = SQLiteSerializedLiveServerThread

    @contextmanager
    def browser(self, client, width):
        from playwright.sync_api import sync_playwright
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': width, 'height': 844}, locale='zh-CN', timezone_id='Asia/Shanghai')
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key, 'url': self.live_server_url}])
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                page = context.new_page()
                errors, failed_assets = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: failed_assets.append(response.url)
                        if '/static/' in response.url and response.status >= 400 else None)
                yield page
                self.assertEqual(errors, [])
                self.assertEqual(failed_assets, [])
            finally:
                browser.close()

    def capture(self, page, name):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), page.viewport_size['width'])
        directory = os.environ.get('PHR_GLUCOSE_BROWSER_ARTIFACT_DIR')
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / name), full_page=True)

    def flow(self, width):
        from playwright.sync_api import expect
        client, patient = _patient(get_user_model(), f'glucose-browser-{width}')
        for measured, value in [('2026-08-02T09:10:23', '7.1'), ('2026-08-02T09:10:23', '7.3'), ('2026-08-03T06:12:34', '6.9')]:
            create_record(patient, patient.account, values(value=value, measured_local=measured), source_kind='METER', creation_key=uuid4())
        with self.browser(client, width) as page:
            page.goto(self.live_server_url + f'/glucose/?patient={patient.pk}', wait_until='networkidle')
            page.get_by_role('link', name='记录自测血糖', exact=True).click()
            page.locator('#id_source_kind').select_option('METER')
            page.get_by_label('原始血糖结果:', exact=True).fill('180')
            page.get_by_label('原始单位:', exact=True).select_option('mg/dL')
            page.get_by_label('测量时间:', exact=True).fill('2026-08-02T06:12:34')
            page.get_by_label('原始时间精度:', exact=True).select_option('SECOND')
            page.get_by_label('原文或本人确认的时段:', exact=True).select_option('FASTING')
            self.capture(page, f'entry-{width}.png')
            page.get_by_role('button', name='保存记录', exact=True).click()
            expect(page.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
            expect(page.get_by_text('180 mg/dL', exact=True).first).to_be_visible()
            expect(page.get_by_text('记录人：本人', exact=False)).to_be_visible()
            record = _db(lambda: GlucoseRecord.objects.get(patient=patient, current_data__raw_value='180'))
            self.assertEqual(record.current_data['normalized_value'], '9.9918')
            page.get_by_role('link', name='更正记录或补充时间', exact=True).click()
            self.assertEqual(page.get_by_label('测量时间:', exact=True).input_value(), '2026-08-02T06:12:34')
            page.get_by_label('原始血糖结果:', exact=True).fill('181')
            page.get_by_role('button', name='保存更正', exact=True).click()
            expect(page.get_by_text('181 mg/dL', exact=True).first).to_be_visible()
            page.get_by_role('button', name='删除记录', exact=True).click()
            expect(page.get_by_text('已删除', exact=True)).to_be_visible()
            page.get_by_role('button', name='撤销上次操作', exact=True).click()
            expect(page.get_by_text('已删除', exact=True)).to_have_count(0)
            page.goto(self.live_server_url + f'/glucose/?patient={patient.pk}', wait_until='networkidle')
            expect(page.locator('.glucose-chart')).to_have_count(1)
            expect(page.locator('[data-glucose-record]')).to_have_count(4)
            expect(page.locator('.glucose-legend li')).to_have_count(2)
            self.capture(page, f'history-{width}.png')
            details = page.locator('.glucose-heatmap details').first
            expect(details.locator('summary')).to_have_text('查看 3 条记录')
            details.locator('summary').focus()
            page.keyboard.press('Enter')
            self.assertTrue(details.evaluate('element => element.open'))
            expect(details.locator('a')).to_have_count(3)
            table = page.locator('.glucose-table-scroll').first
            table.focus()
            page.keyboard.press('End')
            if width == 360:
                self.assertGreater(table.evaluate('element => element.scrollWidth'), table.evaluate('element => element.clientWidth'))
            table.evaluate('element => element.scrollLeft = 0')
            details.locator('a').first.focus()
            page.keyboard.press('Enter')
            expect(page.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
            self.capture(page, f'detail-{width}.png')
            page.goto(self.live_server_url + f'/glucose/?patient={patient.pk}', wait_until='networkidle')
            page.locator('.glucose-filter-panel > summary').click()
            page.locator('#id_start').fill('2026-08-03')
            page.locator('#id_end').fill('2026-08-03')
            page.get_by_role('button', name='筛选', exact=True).click()
            expect(page.locator('[data-glucose-record]')).to_have_count(1)
            expect(page.locator('.glucose-filter-panel > summary')).to_contain_text('已应用')
        record.refresh_from_db()
        self.assertEqual(record.original_data['raw_value'], '180')
        self.assertEqual(record.current_data['raw_value'], '181')
        self.assertEqual(record.current_data['local_time'], '2026-08-02T06:12:34')
        self.assertEqual(record.revision_number, 3)

    def test_desktop_entry_units_multiple_days_and_keyboard(self):
        self.flow(1280)

    def test_phone_entry_seconds_duplicate_cells_and_horizontal_table(self):
        self.flow(360)

    def test_phone_original_import_timezone_confirmation_and_nursing_summary(self):
        from playwright.sync_api import expect
        client, patient, document, _, observation = lab_source(get_user_model())
        content = _pdf_bytes(page_count=1)
        type(document).objects.filter(pk=document.pk).update(sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content))
        store = InMemoryObjectStore()
        store.objects[document.original_object_key] = content
        with patch('apps.documents.views.originals.get_object_store', lambda: store), patch('apps.labs.views.get_object_store', lambda: store), self.browser(client, 360) as page:
            page.goto(self.live_server_url + f'/glucose/import/labs/{observation.pk}/?patient={patient.pk}', wait_until='networkidle')
            original = page.frame_locator('iframe').locator('img')
            expect(original).to_have_js_property('complete', True)
            self.assertGreater(original.evaluate('element => element.naturalWidth'), 0)
            self.assertEqual(page.locator('#id_timezone').input_value(), '')
            self.assertFalse(page.locator('#id_confirm_timezone').is_checked())
            page.locator('#id_checked_original').check()
            self.capture(page, 'lab-confirmation-360.png')
            page.get_by_role('button', name='核对后保存', exact=True).click()
            expect(page.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
            record = _db(lambda: GlucoseRecord.objects.get(patient=patient))
            self.assertIsNone(record.measured_at)
            page.get_by_role('link', name='更正记录或补充时间', exact=True).click()
            page.locator('#id_timezone').fill('Asia/Shanghai')
            page.locator('#id_confirm_timezone').check()
            page.get_by_role('button', name='保存更正', exact=True).click()
            expect(page.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
            page.goto(self.live_server_url + f'/glucose/import/nursing/{document.pk}/1/?patient={patient.pk}', wait_until='networkidle')
            expect(page.locator('.glucose-original')).to_have_js_property('complete', True)
            self.assertGreater(page.locator('.glucose-original').evaluate('element => element.naturalWidth'), 0)
            self.assertEqual(page.locator('#id_measured_local').input_value(), '')
            page.locator('#id_value').fill('7.0–9.0')
            page.locator('#id_unit').fill('mmol/L')
            page.locator('#id_original_excerpt').fill('合成护理汇总：近期血糖7.0–9.0 mmol/L')
            page.locator('#id_measurement_scope').select_option('SUMMARY')
            page.locator('#id_checked_original').check()
            self.capture(page, 'nursing-confirmation-360.png')
            page.get_by_role('button', name='核对后保存', exact=True).click()
            expect(page.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
            page.goto(self.live_server_url + f'/glucose/?patient={patient.pk}', wait_until='networkidle')
            expect(page.locator('[data-glucose-record]')).to_have_count(1)
            expect(page.locator('.glucose-records li')).to_have_count(2)
            self.capture(page, 'mixed-sources-360.png')
        record.refresh_from_db()
        self.assertEqual(record.current_data['timezone_origin'], 'USER_CONFIRMED')
        self.assertEqual(record.original_data['timezone_origin'], 'UNCONFIRMED')
        self.assertEqual(record.current_data['local_time'], '2026-08-02T06:12:34')
        self.assertEqual(record.measured_at.isoformat(), '2026-08-01T22:12:34+00:00')

    def test_phone_corrected_dst_second_requires_explicit_branch_and_keeps_it(self):
        from playwright.sync_api import expect
        client, patient = _patient(get_user_model(), 'glucose-browser-dst')
        record = create_record(patient, patient.account, values(timezone='Europe/Berlin'), creation_key=uuid4()).record
        with self.browser(client, 360) as page:
            url = self.live_server_url + f'/glucose/{record.pk}/edit/?patient={patient.pk}'
            page.goto(url, wait_until='networkidle')
            page.locator('#id_measured_local').fill('2026-10-25T02:30:12')
            with page.expect_response(lambda response: response.request.method == 'POST' and '/edit/' in response.url) as failed:
                page.get_by_role('button', name='保存更正', exact=True).click()
            self.assertEqual(failed.value.status, 400)
            expect(page.locator('#id_utc_offset')).to_be_visible()
            self.capture(page, 'dst-offset-choice-360.png')
            page.locator('#id_utc_offset').fill('+01:00')
            page.get_by_role('button', name='保存更正', exact=True).click()
            expect(page.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
            page.goto(url, wait_until='networkidle')
            page.locator('#id_value').fill('8.1')
            page.get_by_role('button', name='保存更正', exact=True).click()
            expect(page.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
        record.refresh_from_db()
        self.assertEqual(record.measured_at.isoformat(), '2026-10-25T01:30:12+00:00')
        self.assertEqual(record.current_data['raw_value'], '8.1')
        self.assertEqual(record.revision_number, 2)
