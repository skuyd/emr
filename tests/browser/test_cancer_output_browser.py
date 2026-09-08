import hashlib
import io
import json
import os
from pathlib import Path
import re
from unittest.mock import patch
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.exports.formats import read_structured_data
from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from apps.patients.models import PatientShare
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_cancer_ordering_browser as harness
from tests.browser.test_phase_three_browser import _db
from tests.cancer_ordering.test_services import _collect, _row, _select
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestCancerOutputBrowser(SQLiteSerializedStaticLiveServerTestCase):
    browser = harness.TestCancerOrderingBrowser.browser
    capture = harness.TestCancerOrderingBrowser.capture

    def test_selected_pending_statement_and_manual_choice_reach_zip_and_phone_share_then_expire(self):
        from playwright.sync_api import expect
        from pypdf import PdfReader

        client, patient = _patient(get_user_model(), 'cancer-selected-browser-owner')
        reader_client, _ = _patient(get_user_model(), 'cancer-selected-browser-reader')
        document, _ = _collect(patient, ('病理诊断：右肺上叶浸润性腺癌 pT2aN1M0 ⅢA期。', '既往病史：胰腺癌。'))
        _select(patient, 'MANUAL_PROFILE', profile='PANCREAS')
        identity = _row(patient)['id']
        store = InMemoryObjectStore()
        with patch('apps.exports.views.safe_enqueue_export', return_value=None), patch('apps.exports.views.get_object_store', return_value=store), self.browser(client, 1280) as owner:
            owner.goto(self.live_server_url + f'/visit/?patient={patient.pk}', wait_until='networkidle')
            self.assertEqual(owner.locator('input[name="cancer_candidate_ids"]:checked').count(), 0)
            self.assertFalse(owner.locator('input[name="include_indicator_ordering"]').is_checked())
            owner.locator('select[name="mode"]').select_option('documents')
            for option in owner.locator('input[name="sections"]').all():
                option.uncheck()
            owner.locator('input[name="sections"][value="cancer_ordering"]').check()
            owner.locator('input[name="details"]').check()
            owner.locator(f'input[name="cancer_candidate_ids"][value="{identity}"]').check()
            owner.locator('input[name="include_indicator_ordering"]').check()
            owner.get_by_role('button', name='预览内容与导出清单', exact=True).focus()
            owner.keyboard.press('Enter')
            expect(owner.get_by_role('heading', name='确认本次内容', exact=True)).to_be_visible()
            expect(owner.locator('main')).to_contain_text('右肺上叶浸润性腺癌')
            expect(owner.locator('main')).to_contain_text('待核对')
            expect(owner.locator('main')).to_contain_text('胰腺癌指标顺序')
            self.assertNotIn('pT2aN1M0', owner.locator('main').inner_text())
            self.capture(owner, 'selected-output-preview-1280.png')
            owner.get_by_label('导出格式:', exact=True).select_option('zip')
            owner.locator('input[name="parts"][value="csv"]').check()
            owner.get_by_role('button', name='确认清单并生成', exact=True).click()
            expect(owner.get_by_text('正在准备文件。', exact=False)).to_be_visible()
            job = _db(lambda: ExportJob.objects.get(patient=patient))
            _db(lambda: generate_export(job.pk, store))
            owner.reload(wait_until='networkidle')
            with owner.expect_download() as downloaded:
                owner.get_by_role('link', name='下载 records.zip', exact=True).click()
            with zipfile.ZipFile(downloaded.value.path()) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                for row in manifest['files']:
                    content = archive.read(row['path'])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), row['sha256'])
                    self.assertEqual(len(content), row['byte_size'])
                data = read_structured_data(archive.read('records.json'))
                self.assertEqual(data['schema_version'], '1.5')
                self.assertEqual(data['documents'], [])
                self.assertEqual([row['id'] for row in data['cancer_candidates']], [identity])
                self.assertEqual(data['cancer_candidates'][0]['source']['state'], 'OMITTED')
                self.assertEqual(data['indicator_ordering'][0]['mode'], 'MANUAL_PROFILE')
                self.assertIn('csv/cancer_candidates.csv', archive.namelist())
                pdf = PdfReader(io.BytesIO(archive.read('visit-card.pdf')))
                text = '\n'.join(page.extract_text() for page in pdf.pages)
                self.assertIn('右肺上叶浸润性腺癌', text)
                self.assertIn('待核对', text)
                self.assertIn('胰腺癌指标顺序', text)
                self.assertNotIn('pT2aN1M0', text + json.dumps(data, ensure_ascii=False))
                self.assertFalse(any(name.startswith('originals/') for name in archive.namelist()))
            folder = os.environ.get('PHR_CANCER_BROWSER_ARTIFACT_DIR')
            if folder:
                downloaded.value.save_as(str(Path(folder) / 'selected-cancer-output.zip'))
            owner.goto(self.live_server_url + f'/patients/{patient.pk}/shares/', wait_until='networkidle')
            self.assertEqual(owner.locator('input[name="cancer_candidate_ids"]:checked').count(), 0)
            for option in owner.locator('input[name="sections"]').all():
                option.uncheck()
            owner.locator('input[name="sections"][value="cancer_ordering"]').check()
            owner.locator(f'input[name="cancer_candidate_ids"][value="{identity}"]').check()
            owner.locator('input[name="include_indicator_ordering"]').check()
            owner.get_by_role('button', name='生成分享链接', exact=True).click()
            link = owner.get_by_label('分享链接', exact=True).input_value()
            context = owner.context.browser.new_context(viewport={'width': 360, 'height': 844}, locale='zh-CN')
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': reader_client.session.session_key, 'url': self.live_server_url}])
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
            reader = context.new_page()
            try:
                reader.goto(link, wait_until='domcontentloaded')
                reader.wait_for_url(re.compile(r'.*/shared/[0-9a-f-]{36}/$'), wait_until='domcontentloaded')
                expect(reader.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
                expect(reader.locator('main')).to_contain_text('右肺上叶浸润性腺癌')
                expect(reader.locator('main')).to_contain_text('待核对')
                expect(reader.locator('main')).to_contain_text('胰腺癌指标顺序')
                self.assertNotIn('#', reader.url)
                self.assertNotIn('pT2aN1M0', reader.locator('main').inner_text())
                self.assertEqual(reader.get_by_role('link', name='查看这份原件', exact=True).count(), 0)
                shared_id = reader.url.rstrip('/').rsplit('/', 1)[1]
                self.assertEqual(reader.evaluate('async (url) => (await fetch(url)).status',
                    f'/shared/{shared_id}/documents/{document.pk}/'), 404)
                self.capture(reader, 'selected-output-share-360.png')
                owner.goto(self.live_server_url + f'/cancer-ordering/?patient={patient.pk}', wait_until='networkidle')
                owner.get_by_label('排列方式:', exact=True).select_option('GENERAL')
                owner.get_by_role('button', name='保存显示顺序', exact=True).focus()
                owner.keyboard.press('Enter')
                expect(owner.get_by_role('heading', name='当前显示顺序：通用顺序', exact=True)).to_be_visible()
                reader.evaluate("window.dispatchEvent(new Event('pageshow'))")
                expect(reader.get_by_role('alert')).to_contain_text('分享已失效')
                self.assertNotIn('右肺上叶浸润性腺癌', reader.locator('main').inner_text())
                response = owner.goto(self.live_server_url + f'/visit/{job.pk}/download/?patient={patient.pk}', wait_until='networkidle')
                self.assertEqual(response.status, 409)
            finally:
                context.close()
        share = PatientShare.objects.get(patient=patient)
        self.assertEqual(share.snapshot, {})
        self.assertIsNotNone(share.invalidated_at)
