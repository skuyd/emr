from contextlib import contextmanager
import hashlib
import csv
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.glucose.models import GlucoseRecord
from apps.glucose.services import create_record
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient, _pdf_bytes
from tests.glucose.factories import lab_source
from tests.glucose.test_forms import values


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestGlucoseBrowser(SQLiteSerializedStaticLiveServerTestCase):

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

    def test_selected_actual_zip_and_phone_share_keep_raw_units_then_stop_after_correction(self):
        from apps.exports.models import ExportJob
        from apps.exports.services import generate_export
        from apps.patients.models import PatientShare
        from playwright.sync_api import expect
        from pypdf import PdfReader

        client, patient = _patient(get_user_model(), 'glucose-output-browser-owner')
        reader_client, reader_patient = _patient(get_user_model(), 'glucose-output-browser-reader')
        chosen = create_record(patient, patient.account, values(value='180', unit='mg/dL', notes='本次选定血糖备注'), creation_key=uuid4()).record
        create_record(patient, patient.account, values(notes='未选择的血糖私密备注'), creation_key=uuid4())
        store = InMemoryObjectStore()
        with patch('apps.exports.views.safe_enqueue_export', return_value=None), patch('apps.exports.views.get_object_store', return_value=store), self.browser(client, 1280) as owner:
            owner.goto(self.live_server_url + f'/visit/?patient={patient.pk}', wait_until='networkidle')
            self.assertEqual(owner.locator('input[name=glucose_record_ids]:checked').count(), 0)
            owner.locator(f'input[name=glucose_record_ids][value="{chosen.pk}"]').check()
            owner.get_by_label('允许附页：正文超出 A4 一页时将完整明细放入附页').check()
            owner.get_by_role('button', name='预览内容与导出清单', exact=True).click()
            expect(owner.get_by_role('heading', name='确认本次内容', exact=True)).to_be_visible()
            expect(owner.get_by_text('1 条选定血糖记录。', exact=True)).to_be_visible()
            expect(owner.locator('main')).to_contain_text('180 mg/dL')
            expect(owner.locator('main')).to_contain_text('2026-08-02T06:12:34')
            self.assertNotIn('未选择的血糖私密备注', owner.locator('main').inner_text())
            self.capture(owner, 'selected-preview-desktop.png')
            owner.get_by_label('导出格式:', exact=True).select_option('zip')
            owner.locator('input[name=parts][value=csv]').check()
            owner.get_by_role('button', name='确认清单并生成', exact=True).click()
            expect(owner.get_by_text('正在准备文件。', exact=False)).to_be_visible()
            job = _db(lambda: ExportJob.objects.get(patient=patient))
            _db(lambda: generate_export(job.pk, store))
            owner.reload(wait_until='networkidle')
            with owner.expect_download() as downloaded:
                owner.get_by_role('link', name='下载 records.zip', exact=True).click()
            with zipfile.ZipFile(downloaded.value.path()) as archive:
                self.assertFalse(any(name.startswith('originals/') for name in archive.namelist()))
                data = json.loads(archive.read('records.json'))
                self.assertEqual(data['schema_version'], '1.6')
                self.assertEqual(data['documents'], [])
                self.assertEqual(data['scope']['glucose_record_ids'], [str(chosen.pk)])
                self.assertEqual([row['id'] for row in data['glucose_records']], [str(chosen.pk)])
                record = data['glucose_records'][0]
                self.assertEqual(record['data']['raw_value'], '180')
                self.assertEqual(record['data']['normalized_value'], '9.9918')
                self.assertEqual(record['original_data']['raw_unit'], 'mg/dL')
                self.assertEqual(record['data']['time_precision'], 'SECOND')
                self.assertEqual(record['data']['local_time'], '2026-08-02T06:12:34')
                rows = list(csv.DictReader(io.StringIO(archive.read('csv/glucose_records.csv').decode('utf-8-sig'))))
                self.assertEqual([row['id'] for row in rows], [str(chosen.pk)])
                self.assertEqual(rows[0]['raw_unit'], 'mg/dL')
                pdf = PdfReader(io.BytesIO(archive.read('visit-card.pdf')))
                printable = '\n'.join(page.extract_text() for page in pdf.pages)
                self.assertIn('180 mg/dL', printable)
                self.assertIn('06:12:34', printable)
                self.assertNotIn('未选择的血糖私密备注', printable + json.dumps(data, ensure_ascii=False))
            directory = os.environ.get('PHR_GLUCOSE_BROWSER_ARTIFACT_DIR')
            if directory:
                downloaded.value.save_as(str(Path(directory) / 'selected-glucose.zip'))
            owner.goto(self.live_server_url + f'/patients/{patient.pk}/shares/', wait_until='networkidle')
            self.assertEqual(owner.locator('input[name=glucose_record_ids]:checked').count(), 0)
            owner.locator(f'input[name=glucose_record_ids][value="{chosen.pk}"]').check()
            owner.get_by_role('button', name='生成分享链接', exact=True).click()
            link = owner.get_by_label('分享链接', exact=True).input_value()
            context = owner.context.browser.new_context(viewport={'width': 360, 'height': 844}, locale='zh-CN')
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': reader_client.session.session_key, 'url': self.live_server_url}])
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
            reader = context.new_page()
            reader_errors = []
            reader.on('pageerror', lambda error: reader_errors.append(str(error)))
            try:
                reader.goto(link, wait_until='domcontentloaded')
                expect(reader.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
                expect(reader.get_by_role('heading', name='选定血糖记录', exact=True)).to_be_visible()
                expect(reader.get_by_text('备注：本次选定血糖备注', exact=True)).to_be_visible()
                expect(reader.locator('main')).to_contain_text('180 mg/dL')
                expect(reader.locator('main')).to_contain_text('06:12:34')
                self.assertNotIn('未选择的血糖私密备注', reader.locator('main').inner_text())
                self.assertEqual(reader.locator('a[href*="/glucose/"]').count(), 0)
                self.assertEqual(reader.get_by_role('link', name='下载原件', exact=True).count(), 0)
                self.assertEqual(reader.evaluate('async (url) => (await fetch(url)).status', f'/glucose/{chosen.pk}/?patient={patient.pk}'), 404)
                self.capture(reader, 'selected-share-phone.png')
                owner.goto(self.live_server_url + f'/glucose/{chosen.pk}/edit/?patient={patient.pk}', wait_until='networkidle')
                owner.get_by_label('原始血糖结果:', exact=True).fill('181')
                owner.get_by_role('button', name='保存更正', exact=True).click()
                expect(owner.get_by_role('heading', name='当前记录', exact=True)).to_be_visible()
                reader.evaluate("window.dispatchEvent(new Event('pageshow'))")
                expect(reader.get_by_role('alert')).to_contain_text('分享已失效')
                self.assertNotIn('本次选定血糖备注', reader.locator('main').inner_text())
                response = owner.goto(self.live_server_url + f'/visit/{job.pk}/download/?patient={patient.pk}', wait_until='networkidle')
                self.assertEqual(response.status, 409)
                self.assertEqual(reader_errors, [])
            finally:
                context.close()
        share = PatientShare.objects.get(patient=patient)
        self.assertEqual(share.snapshot, {})
        self.assertIsNotNone(share.invalidated_at)
        self.assertNotEqual(reader_patient.account_id, patient.account_id)

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

    def test_phone_display_timezone_filter_preserves_original_and_unknown_times(self):
        from playwright.sync_api import expect
        from apps.glucose.services import import_lab_record
        from apps.glucose.sources import preview_lab
        client, patient, _, _, observation = lab_source(get_user_model(), marker='glucose-browser-display-zone')
        candidate = preview_lab(patient, patient.account, observation.pk)
        import_lab_record(patient, patient.account, observation.pk, expected_source=candidate['source_fingerprint'],
                          checked_original=True, creation_key=uuid4())
        early = create_record(patient, patient.account, values(measured_local='2026-08-02T00:30:12'),
                              source_kind='METER', creation_key=uuid4()).record
        create_record(patient, patient.account, values(measured_local='2026-08-02T09:30:12'),
                      source_kind='METER', creation_key=uuid4())
        with self.browser(client, 360) as page:
            page.goto(self.live_server_url + f'/glucose/?patient={patient.pk}', wait_until='networkidle')
            page.locator('.glucose-filter-panel > summary').click()
            page.locator('#id_display_timezone').fill('UTC')
            page.locator('#id_start').fill('2026-08-01')
            page.locator('#id_end').fill('2026-08-01')
            page.get_by_role('button', name='筛选', exact=True).click()
            expect(page.locator('.glucose-records li')).to_have_count(1)
            expect(page.locator('[data-glucose-record]')).to_have_count(1)
            expect(page.locator('.glucose-records')).to_contain_text('2026-08-01 16:30:12')
            expect(page.locator('.glucose-records')).to_contain_text('原记录：2026-08-02 00:30:12')
            expect(page.locator('.glucose-legend')).to_contain_text('2026-08-01')
            self.capture(page, 'display-timezone-360.png')
            page.locator('.glucose-filter-panel > summary').click()
            self.assertEqual(page.locator('#id_display_timezone').input_value(), 'UTC')
            page.locator('#id_start').fill('2026-08-02')
            page.locator('#id_end').fill('2026-08-02')
            page.get_by_role('button', name='筛选', exact=True).click()
            expect(page.locator('.glucose-records li')).to_have_count(2)
            expect(page.locator('[data-glucose-record]')).to_have_count(1)
            expect(page.locator('.glucose-records')).to_contain_text('没有确定时刻')
            expect(page.locator('.glucose-records')).to_contain_text('2026-08-02 06:12:34')
            self.capture(page, 'display-timezone-unknown-360.png')
        early.refresh_from_db()
        self.assertEqual(early.current_data['local_time'], '2026-08-02T00:30:12')
        self.assertEqual(early.revision_number, 0)
