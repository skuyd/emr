import json
import os
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from apps.self_records.models import DailyRecord
from apps.self_records.services import create_record
from tests.browser.sqlite_server import SQLiteSerializedLiveServerThread
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.test_detail_viewer import _patient
from tests.documents.fakes import InMemoryObjectStore
from tests.self_records.test_payloads import payload


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestSelfRecordsBrowser(StaticLiveServerTestCase):
    server_thread_class = SQLiteSerializedLiveServerThread

    def _width(self, page):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), page.viewport_size['width'])

    def _flow(self, width):
        from playwright.sync_api import expect, sync_playwright
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), f'daily-browser-{width}')
        artifacts = os.environ.get('PHR_SELF_RECORD_BROWSER_ARTIFACT_DIR')
        folder = Path(artifacts) if artifacts else None
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context = browser.new_context(viewport={'width': width, 'height': 800}, locale='zh-CN', timezone_id='Asia/Shanghai')
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key, 'url': self.live_server_url}])
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
            page = context.new_page()
            errors, missing_assets = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: missing_assets.append(response.url) if '/static/' in response.url and response.status >= 400 else None)
            page.goto(self.live_server_url + f'/?patient={patient.pk}', wait_until='networkidle')
            page.get_by_role('link', name='记录体重、体温或症状', exact=True).click()
            expect(page.get_by_text('这一范围还没有记录。', exact=True)).to_be_visible()
            page.get_by_role('link', name='记一条', exact=True).click()
            page.wait_for_load_state('networkidle')
            self._width(page)
            self.assertEqual(page.get_by_label('所在时区:', exact=True).input_value(), 'Asia/Shanghai')
            self.assertTrue(page.get_by_label('测量或发生时间:', exact=True).input_value())
            if folder:
                page.screenshot(path=str(folder / f'entry-form-{width}.png'), full_page=True)
            started = time.perf_counter()
            page.get_by_label('数值:', exact=True).fill('60.25')
            page.get_by_role('button', name='保存记录', exact=True).click()
            expect(page.get_by_role('heading', name='体重记录', exact=True)).to_be_visible()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 30)
            expect(page.get_by_role('heading', name='当前内容', exact=True)).to_be_visible()
            self._width(page)
            if folder:
                (folder / f'quick-entry-{width}.json').write_text(json.dumps({'viewport_width': width, 'seconds': elapsed,
                    'method': 'Automated Chromium: already open form to saved detail; not a human usability study.'}, indent=2), encoding='utf-8')
            page.get_by_role('link', name='更正记录', exact=True).click()
            page.get_by_label('数值:', exact=True).fill('61.5')
            page.get_by_role('button', name='保存更正', exact=True).click()
            expect(page.get_by_text('61.5 kg', exact=True).first).to_be_visible()
            page.get_by_role('button', name='删除记录（可撤销）', exact=True).click()
            expect(page.get_by_role('heading', name='体重记录（已删除）', exact=True)).to_be_visible()
            page.get_by_role('button', name='撤销最近一次操作', exact=True).click()
            expect(page.get_by_role('heading', name='体重记录', exact=True)).to_be_visible()
            page.get_by_role('link', name='再记一条', exact=True).click()
            page.get_by_role('link', name='体温', exact=True).click()
            page.get_by_label('数值:', exact=True).fill('98.6')
            page.get_by_label('单位:', exact=True).select_option('°F')
            page.get_by_role('button', name='保存记录', exact=True).click()
            expect(page.get_by_role('heading', name='体温记录', exact=True)).to_be_visible()
            page.get_by_role('link', name='再记一条', exact=True).click()
            page.get_by_role('link', name='症状', exact=True).click()
            page.get_by_label('症状名称:', exact=True).fill('乏力')
            page.get_by_label('自述程度（可留空）:', exact=True).fill('步行时明显')
            page.get_by_role('button', name='保存记录', exact=True).click()
            expect(page.get_by_role('heading', name='症状记录', exact=True)).to_be_visible()
            page.goto(self.live_server_url + f'/self-records/?patient={patient.pk}', wait_until='networkidle')
            self.assertEqual(page.locator('.self-record-chart').count(), 3)
            self.assertEqual(page.locator('.self-record-point').count(), 3)
            self._width(page)
            details = page.locator('details').filter(has=page.get_by_text('查看全部 1 条来源', exact=True)).first
            details.locator('summary').focus()
            page.keyboard.press('Enter')
            self.assertTrue(details.evaluate('(element) => element.open'))
            if width == 360:
                # Even a single measurement must be visible without hunting
                # horizontally through an apparently empty chart.
                for point in page.locator('.self-record-point').all():
                    box = point.bounding_box()
                    self.assertGreaterEqual(box['x'], 0)
                    self.assertLessEqual(box['x'] + box['width'], width)
            if folder:
                page.screenshot(path=str(folder / f'history-{width}.png'), full_page=True)
            details.locator('a').click()
            expect(page.get_by_role('heading', name='体重记录', exact=True)).to_be_visible()
            self.assertEqual(errors, [])
            self.assertEqual(missing_assets, [])
            browser.close()
        self.assertEqual(DailyRecord.objects.filter(patient=patient, deleted_at__isnull=True).count(), 3)
        weight = DailyRecord.objects.get(patient=patient, kind='WEIGHT')
        self.assertEqual(weight.revision_number, 3)
        self.assertEqual(weight.original_data['raw_value'], '60.25')
        self.assertEqual(weight.current_data['raw_value'], '61.5')

    def test_desktop_quick_entry_history_and_reversible_correction(self):
        self._flow(1280)

    def test_mobile_quick_entry_symptoms_keyboard_and_actual_source(self):
        self._flow(360)

    def test_record_only_zip_and_limited_mobile_share_stop_after_browser_correction(self):
        from apps.exports.models import ExportJob
        from apps.exports.services import generate_export
        from apps.patients.models import PatientShare
        from playwright.sync_api import expect, sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        owner_client, patient = _patient(get_user_model(), 'daily-package-browser-owner')
        reader_client, reader_patient = _patient(get_user_model(), 'daily-package-browser-reader')
        chosen = create_record(patient, patient.account, payload(kind='TEMPERATURE', value='98.6', unit='°F', notes='本次选定备注'), creation_key=uuid4()).record
        create_record(patient, patient.account, payload(notes='未选择的私密备注'), creation_key=uuid4())
        store = InMemoryObjectStore()
        artifacts = os.environ.get('PHR_SELF_RECORD_BROWSER_ARTIFACT_DIR')
        folder = Path(artifacts) if artifacts else None
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
        with patch('apps.exports.views.safe_enqueue_export', return_value=None), patch('apps.exports.views.get_object_store', return_value=store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            owner_context = browser.new_context(viewport={'width': 1280, 'height': 800}, locale='zh-CN', accept_downloads=True)
            reader_context = browser.new_context(viewport={'width': 360, 'height': 800}, locale='zh-CN')
            errors = []
            for context, client in ((owner_context, owner_client), (reader_context, reader_client)):
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key, 'url': self.live_server_url}])
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                context.on('page', lambda page: page.on('pageerror', lambda error: errors.append(str(error))))
            owner = owner_context.new_page()
            owner.goto(self.live_server_url + f'/visit/?patient={patient.pk}', wait_until='networkidle')
            owner.locator(f'input[name=self_record_ids][value="{chosen.pk}"]').check()
            owner.get_by_label('允许附页：正文超出 A4 一页时将完整明细放入附页').check()
            owner.get_by_role('button', name='预览内容与导出清单', exact=True).click()
            expect(owner.get_by_role('heading', name='确认本次内容', exact=True)).to_be_visible()
            expect(owner.get_by_text('1 条选定日常记录。', exact=True)).to_be_visible()
            self.assertNotIn('未选择的私密备注', owner.locator('main').inner_text())
            owner.get_by_label('导出格式:', exact=True).select_option('zip')
            owner.get_by_role('button', name='确认清单并生成', exact=True).click()
            expect(owner.get_by_text('正在准备文件。', exact=False)).to_be_visible()
            job = _db(lambda: ExportJob.objects.get(patient=patient))
            _db(lambda: generate_export(job.pk, store))
            owner.reload(wait_until='networkidle')
            with owner.expect_download() as downloaded:
                owner.get_by_role('link', name='下载 records.zip', exact=True).click()
            archive_path = Path(downloaded.value.path())
            with zipfile.ZipFile(archive_path) as archive:
                self.assertFalse(any(name.startswith('originals/') for name in archive.namelist()))
                data = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith('records.json'))))
                self.assertEqual([row['id'] for row in data['self_records']], [str(chosen.pk)])
                self.assertEqual(data['self_records'][0]['data']['normalized_value'], '37')
                self.assertTrue(archive.read(next(name for name in archive.namelist() if name.endswith('.pdf'))).startswith(b'%PDF'))
            if folder:
                downloaded.value.save_as(str(folder / 'record-only.zip'))
            owner.goto(self.live_server_url + f'/patients/{patient.pk}/shares/', wait_until='networkidle')
            owner.locator(f'input[name=self_record_ids][value="{chosen.pk}"]').check()
            owner.get_by_role('button', name='生成分享链接', exact=True).click()
            link = owner.get_by_label('分享链接', exact=True).input_value()
            reader = reader_context.new_page()
            # The landing page immediately exchanges the token and navigates.
            # Wait for the destination content instead of idle on the old page.
            reader.goto(link, wait_until='domcontentloaded')
            expect(reader.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
            expect(reader.get_by_text('备注：本次选定备注', exact=True)).to_be_visible()
            self.assertNotIn('未选择的私密备注', reader.locator('main').inner_text())
            self.assertEqual(reader.locator('a[href*="/self-records/"]').count(), 0)
            self.assertEqual(reader.get_by_role('link', name='下载原件', exact=True).count(), 0)
            self.assertEqual(reader.evaluate('async (url) => (await fetch(url)).status', f'/self-records/{chosen.pk}/'), 404)
            self._width(reader)
            if folder:
                reader.screenshot(path=str(folder / 'record-share-phone.png'), full_page=True)
            owner.goto(self.live_server_url + f'/self-records/{chosen.pk}/edit/?patient={patient.pk}', wait_until='networkidle')
            owner.get_by_label('数值:', exact=True).fill('99.5')
            owner.get_by_role('button', name='保存更正', exact=True).click()
            expect(owner.get_by_role('heading', name='体温记录', exact=True)).to_be_visible()
            reader.evaluate("window.dispatchEvent(new Event('pageshow'))")
            expect(reader.get_by_role('alert')).to_contain_text('分享已失效')
            self.assertNotIn('本次选定备注', reader.locator('main').inner_text())
            response = owner.goto(self.live_server_url + f'/visit/{job.pk}/download/', wait_until='networkidle')
            self.assertEqual(response.status, 409)
            self.assertEqual(errors, [])
            browser.close()
        share = PatientShare.objects.get(patient=patient)
        self.assertEqual(share.snapshot, {})
        self.assertIsNotNone(share.invalidated_at)
        self.assertNotEqual(reader_patient.account_id, patient.account_id)
