"""Actual Chromium source review, dynamic member form, ZIP and limited sharing."""
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch
import zipfile

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.documents.backends import get_object_store
from apps.documents.models import Document
from apps.exports.models import ExportJob
from apps.exports.formats import read_structured_data
from apps.exports.services import generate_export
from apps.facts.clinical_extraction import extract_clinical_version
from apps.facts.models import Fact
from apps.lesions.models import Lesion
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_lesion_relations_browser import labelled
from tests.browser.test_phase_three_browser import _db
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.facts.test_scoped_laterality import confirm


def original_report(patient, store):
    from reportlab.pdfgen.canvas import Canvas
    from apps.exports.pdf import _font, FONT

    texts = ['合成医院 CT诊断报告书', '检查日期：2026-09-08 检查项目：胸腹部CT',
             '影像表现：左肺及右肾见结节，长径12mm。', '诊断意见：未选结论正文标记。']
    document, version = parsed_facts(patient, texts, document_type='IMAGING')
    page = document.pages.get()
    output = io.BytesIO()
    _font()
    canvas = Canvas(output, pagesize=(page.width, page.height))
    canvas.setFont(FONT, 16)
    for index, text in enumerate(texts):
        canvas.drawString(page.width * .1, page.height * (.88-index*.02)+5, text)
    canvas.save()
    data = output.getvalue()
    document.sha256, document.byte_size = hashlib.sha256(data).hexdigest(), len(data)
    Document.objects.filter(pk=document.pk).update(sha256=document.sha256, byte_size=document.byte_size)
    version.document = document
    staged = store.put_staging(io.BytesIO(data), expected_size=len(data), expected_sha256=document.sha256)
    store.promote_immutable(staged, document.original_object_key)
    extract_clinical_version(version)
    report = document.clinical_reports.get()
    parent = report.fields.get(field_key='lesion.site')
    for field in report.fields.exclude(field_key='lesion.scoped_laterality'):
        confirm(patient, field)
    return document, report, parent


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestScopedLateralityOutputBrowser(SQLiteSerializedStaticLiveServerTestCase):
    observation_name = '观察 <A>'
    output_name = observation_name

    def test_original_scope_members_selected_zip_and_share_on_phone(self):
        from playwright.sync_api import sync_playwright, expect

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser')
        owner, patient = _patient(get_user_model(), 'scope-output-browser-owner')
        viewer, _ = _patient(get_user_model(), 'scope-output-browser-viewer')
        evidence = os.environ.get('PHR_B3_SCOPE_ARTIFACT_DIR')
        evidence_dir = Path(evidence).resolve() / type(self).__name__ if evidence else None
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='phr-scope-output-browser-') as temporary:
            with override_settings(DOCUMENT_STORAGE_BACKEND='local', DOCUMENT_STORAGE_ROOT=Path(temporary)):
                store = get_object_store()
                document, report, parent = original_report(patient, store)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                    context = browser.new_context(viewport={'width': 390, 'height': 844}, locale='zh-CN', accept_downloads=True)
                    context.add_cookies([{'name': 'sessionid', 'value': owner.session.session_key, 'url': self.live_server_url}])
                    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                    page = context.new_page()
                    errors, server_errors = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('response', lambda response: server_errors.append(response.status) if response.status >= 500 else None)
                    try:
                        page.goto(f'{self.live_server_url}/facts/{parent.pk}/scope/', wait_until='networkidle')
                        expect(page.frame_locator('iframe').locator('[data-viewer-image]')).to_have_js_property('complete', True)
                        for index, site, code in [(0, '左肺', 'LEFT'), (1, '右肾', 'RIGHT')]:
                            if index:
                                page.get_by_role('button', name='增加一个列明部位', exact=True).click()
                            page.locator(f'[name="members-{index}-site_text"]').fill(site)
                            page.locator(f'[name="members-{index}-code"]').select_option(code)
                            page.locator(f'[name="members-{index}-sources"]').first.check()
                            page.locator(f'[name="members-{index}-raw_text"]').fill(site)
                        expect(page.locator('[data-scope-member]')).to_have_count(2)
                        page.locator('[name="checked_original"]').check()
                        page.locator('[name="confirm"]').check()
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                        if evidence_dir:
                            page.screenshot(path=str(evidence_dir / 'scope-original-members-phone.png'), full_page=True)
                        page.get_by_role('button', name='保存补录范围', exact=True).click()
                        expect(page.get_by_role('heading', name='侧别范围操作记录', exact=True)).to_be_visible()
                        child = _db(lambda: Fact.objects.get(document=document, field_key='lesion.scoped_laterality', origin='MANUAL'))
                        page.goto(f'{self.live_server_url}/facts/{child.pk}/', wait_until='networkidle')
                        expect(page.locator('main')).to_contain_text('仅限列明部位')
                        expect(page.locator('main')).to_contain_text('左肺')
                        expect(page.locator('main')).to_contain_text('右肾')
                        page.goto(f'{self.live_server_url}/lesions/reports/{report.pk}/{parent.entity_key}/', wait_until='networkidle')
                        labelled(page, '新观察名称').fill(self.observation_name)
                        labelled(page, '我已核对原件，选择为这条观察建立稳定标识').check()
                        page.get_by_role('button', name='建立稳定标识', exact=True).click()
                        expect(page.get_by_role('heading', name=self.observation_name, exact=True)).to_be_visible()
                        lesion = _db(lambda: Lesion.objects.get(patient=patient))
                        dimensions = _db(lambda: Fact.objects.get(document=document, field_key='lesion.dimensions'))
                        chosen = [str(parent.pk), str(child.pk), str(dimensions.pk)]
                        response = page.goto(self.live_server_url + reverse('exports:prepare'), wait_until='networkidle')
                        self.assertEqual(response.status, 200)
                        page.locator('[name="mode"]').select_option('documents')
                        page.locator('summary').filter(has_text='按资料勾选').click()
                        page.locator(f'[name="document_ids"][value="{document.pk}"]').check()
                        page.locator(f'[name="lesion_ids"][value="{lesion.pk}"]').check()
                        page.get_by_text('选择结构化报告与字段', exact=True).click()
                        page.locator('[name="custom_clinical_fields"]').check()
                        for identity in chosen:
                            page.locator(f'[name="clinical_field_ids"][value="{identity}"]').check()
                        page.locator('[name="details"]').check()
                        page.get_by_role('button', name='预览内容与导出清单', exact=True).click()
                        expect(page.get_by_role('heading', name='确认本次内容', exact=True)).to_be_visible()
                        expect(page.locator('main')).to_contain_text('人工确认的观察分组')
                        expect(page.locator('main')).to_contain_text(self.output_name)
                        if self.output_name != self.observation_name:
                            expect(page.locator('main')).not_to_contain_text('LESION_BROWSER_SECRET')
                        page.locator('[name="format"]').select_option('zip')
                        for checkbox in page.locator('[name="parts"]').all():
                            checkbox.set_checked(checkbox.get_attribute('value') in ('json', 'csv'))
                        with patch('apps.exports.views.safe_enqueue_export', lambda *_: None):
                            page.get_by_role('button', name='确认清单并生成', exact=True).click()
                            page.wait_for_load_state('networkidle')
                        job = _db(lambda: ExportJob.objects.get(patient=patient))
                        _db(lambda: generate_export(job.pk, store))
                        page.reload(wait_until='networkidle')
                        with page.expect_download() as downloaded:
                            page.get_by_role('link', name='下载 ', exact=False).click()
                        path = Path(downloaded.value.path())
                        with zipfile.ZipFile(path) as archive:
                            data = read_structured_data(archive.read('records.json'))
                            self.assertEqual(len(data['lesions']), 1)
                            self.assertEqual(data['lesions'][0]['name'], self.output_name)
                            self.assertNotIn('LESION_BROWSER_SECRET', archive.read('records.json').decode())
                            self.assertEqual(len(data['lesion_observations']), 1)
                            field = next(row for row in data['clinical_fields'] if row['id'] == str(child.pk))
                            self.assertEqual(field['content']['value']['scope'], 'NAMED_MEMBERS_ONLY')
                            self.assertEqual(len(field['content']['value']['members']), 2)
                            self.assertNotIn('未选结论正文标记', archive.read('records.json').decode())
                            self.assertIn('lesions.csv', {name.split('/')[-1] for name in archive.namelist()})
                        if evidence_dir:
                            downloaded.value.save_as(str(evidence_dir / 'scope-selected-output.zip'))
                            page.screenshot(path=str(evidence_dir / 'scope-export-phone.png'), full_page=True)
                        page.goto(f'{self.live_server_url}/patients/{patient.pk}/shares/', wait_until='networkidle')
                        page.locator(f'[name="document_ids"][value="{document.pk}"]').check()
                        page.locator(f'[name="lesion_ids"][value="{lesion.pk}"]').check()
                        for identity in chosen:
                            page.locator(f'[name="clinical_field_ids"][value="{identity}"]').check()
                        for checkbox in page.locator('[name="sections"]').all():
                            checkbox.set_checked(checkbox.get_attribute('value') == 'imaging')
                        page.get_by_role('button', name='生成分享链接', exact=True).click()
                        link = page.locator('[name="share_link"]').input_value()
                        reader = browser.new_context(viewport={'width': 390, 'height': 844}, locale='zh-CN')
                        reader.add_cookies([{'name': 'sessionid', 'value': viewer.session.session_key, 'url': self.live_server_url}])
                        reader.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                        shared = reader.new_page()
                        shared.on('pageerror', lambda error: errors.append(str(error)))
                        shared.goto(link, wait_until='domcontentloaded')
                        shared.wait_for_url(re.compile(r'/shared/[0-9a-f-]{36}/$'), wait_until='domcontentloaded')
                        if evidence_dir:
                            shared.screenshot(path=str(evidence_dir / 'scope-share-entry-phone.png'), full_page=True)
                        expect(shared.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
                        self.assertNotIn('#', shared.url)
                        self.assertIsNone(shared.evaluate("sessionStorage.getItem('phr:pending-share')"))
                        expect(shared.locator('main')).to_contain_text('人工确认的观察分组')
                        expect(shared.locator('main')).to_contain_text(self.output_name)
                        expect(shared.locator('main')).not_to_contain_text('LESION_BROWSER_SECRET')
                        expect(shared.locator('main')).to_contain_text('仅限列明部位')
                        expect(shared.locator('main')).not_to_contain_text('未选结论正文标记')
                        self.assertEqual(shared.locator('a[href*="/facts/"], a[href*="/lesions/"], iframe').count(), 0)
                        self.assertFalse(shared.evaluate('document.documentElement.scrollWidth > innerWidth'))
                        if evidence_dir:
                            shared.screenshot(path=str(evidence_dir / 'scope-selected-share-phone.png'), full_page=True)
                        page.goto(f'{self.live_server_url}/facts/{parent.pk}/', wait_until='networkidle')
                        page.get_by_role('button', name='撤销确认', exact=True).click()
                        response = shared.reload(wait_until='networkidle')
                        self.assertEqual(response.status, 410)
                        expect(shared.locator('main')).not_to_contain_text(self.output_name)
                        self.assertEqual(errors, [])
                        self.assertEqual(server_errors, [])
                    finally:
                        browser.close()


class TestCloudOmittedScopedLateralityOutputBrowser(TestScopedLateralityOutputBrowser):
    observation_name = '观察 https://images.example.invalid/view?key=LESION_BROWSER_SECRET#entry'
    output_name = '观察 ［已省略外部访问内容］'
