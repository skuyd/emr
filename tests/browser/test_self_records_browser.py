import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.self_records.services import create_record
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.test_detail_viewer import _patient
from tests.documents.fakes import InMemoryObjectStore
from tests.self_records.test_payloads import payload


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestSelfRecordsBrowser(SQLiteSerializedStaticLiveServerTestCase):
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
        chosen = create_record(patient, patient.account, payload(kind='TEMPERATURE', value='98.6', unit='°F'), creation_key=uuid4()).record
        create_record(patient, patient.account, payload(kind='SYMPTOM', symptom_name='UNSELECTED_DAILY_CANARY'), creation_key=uuid4())
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
            self.assertNotIn('UNSELECTED_DAILY_CANARY', owner.locator('main').inner_text())
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
            expect(reader.get_by_text('98.6 °F', exact=False)).to_be_visible()
            self.assertNotIn('UNSELECTED_DAILY_CANARY', reader.locator('main').inner_text())
            self.assertEqual(reader.locator('a[href*="/self-records/"]').count(), 0)
            self.assertEqual(reader.get_by_role('link', name='下载原件', exact=True).count(), 0)
            self.assertEqual(reader.evaluate('async (url) => (await fetch(url)).status', f'/self-records/{chosen.pk}/'), 404)
            self.assertLessEqual(reader.evaluate('document.documentElement.scrollWidth'), 360)
            if folder:
                reader.screenshot(path=str(folder / 'record-share-phone.png'), full_page=True)
            owner.goto(self.live_server_url + f'/self-records/{chosen.pk}/edit/?patient={patient.pk}', wait_until='networkidle')
            owner.get_by_label('体温', exact=True).fill('99.5')
            owner.get_by_role('button', name='保存更正', exact=True).click()
            expect(owner.get_by_role('heading', name='记一条', exact=True)).to_be_visible()
            reader.evaluate("window.dispatchEvent(new Event('pageshow'))")
            expect(reader.get_by_role('alert')).to_contain_text('分享已失效')
            self.assertNotIn('98.6 °F', reader.locator('main').inner_text())
            response = owner.goto(self.live_server_url + f'/visit/{job.pk}/download/', wait_until='networkidle')
            self.assertEqual(response.status, 409)
            self.assertEqual(errors, [])
            browser.close()
        share = PatientShare.objects.get(patient=patient)
        self.assertEqual(share.snapshot, {})
        self.assertIsNotNone(share.invalidated_at)
        self.assertNotEqual(reader_patient.account_id, patient.account_id)
