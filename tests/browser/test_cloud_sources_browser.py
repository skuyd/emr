from contextlib import contextmanager
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.cloud_imaging.models import CloudImagingScan, CloudImagingSource
from apps.cloud_imaging.scan_services import run_scan
from apps.patients.models import PatientMembership
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.cloud_imaging.factories import stored_document
from tests.cloud_imaging.test_source_services import SECOND_URL
from tests.documents.test_detail_viewer import _patient


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestCloudSourcesBrowser(SQLiteSerializedStaticLiveServerTestCase):
    @contextmanager
    def browser(self, client, width):
        from playwright.sync_api import sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': width, 'height': 844}, locale='zh-CN')
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key, 'url': self.live_server_url}])
                external, errors, assets = [], [], []
                def private_route(route):
                    if route.request.url.startswith(self.live_server_url + '/'):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()
                context.route('**/*', private_route)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: assets.append(response.url)
                        if '/static/' in response.url and response.status >= 400 else None)
                yield page
                self.assertEqual(errors, [])
                self.assertEqual(assets, [])
                self.assertEqual(external, [])
            finally:
                browser.close()

    def capture(self, page, filename):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), page.viewport_size['width'])
        directory = os.environ.get('PHR_CLOUD_BROWSER_ARTIFACT_DIR')
        if directory:
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / filename), full_page=True)

    def test_desktop_actual_scan_original_review_correction_exclusion_and_undo(self):
        from playwright.sync_api import expect

        client, patient = _patient(get_user_model(), 'cloud-browser-desktop')
        document, _, store = stored_document(patient, pdf=True)
        with patch('apps.cloud_imaging.tasks.safe_enqueue_scan', return_value=False), patch('apps.documents.views.originals.get_object_store', return_value=store), self.browser(client, 1280) as page:
            page.goto(self.live_server_url + f'/records/{document.pk}/cloud-imaging/?patient={patient.pk}', wait_until='networkidle')
            page.get_by_role('button', name='扫描本份原件', exact=True).click()
            expect(page.get_by_text('等待扫描', exact=False)).to_be_visible()
            scan = _db(lambda: CloudImagingScan.objects.get(document=document))
            _db(lambda: run_scan(scan.pk, store))
            page.reload(wait_until='networkidle')
            page.get_by_role('link', name='二维码来源', exact=True).click()
            expect(page.get_by_role('heading', name='核对结果', exact=True)).to_be_visible()
            source = _db(lambda: CloudImagingSource.objects.get(document=document))
            self.capture(page, 'pending-desktop.png')
            page.get_by_role('link', name='查看原件第 1 页', exact=True).click()
            page.locator('[data-viewer-image]').wait_for(state='visible')
            page.wait_for_function('document.querySelector("[data-viewer-image]").naturalWidth > 0')
            expect(page.locator('[data-viewer-highlight]')).to_be_visible()
            self.capture(page, 'original-location-desktop.png')
            page.go_back(wait_until='networkidle')
            page.get_by_label('我已对照当前原页核对来源').check()
            page.get_by_role('button', name='核对并提交', exact=True).click()
            expect(page.get_by_text('原页来源已核对。', exact=False)).to_be_visible()
            self.capture(page, 'confirmed-desktop.png')
            page.locator('#id_action').select_option('CORRECT')
            page.get_by_label('原页上的完整地址:').fill(SECOND_URL)
            page.get_by_label('我已对照当前原页核对来源').check()
            page.get_by_role('button', name='核对并提交', exact=True).click()
            page.locator('#id_action').select_option('EXCLUDE')
            page.get_by_role('button', name='核对并提交', exact=True).click()
            expect(page.locator('main')).to_contain_text('已排除')
            page.locator('#id_action').select_option('UNDO')
            page.get_by_role('button', name='核对并提交', exact=True).click()
            expect(page.locator('main')).to_contain_text('待核对')
            row = _db(lambda: CloudImagingSource.objects.get(pk=source.pk))
            self.assertEqual((row.status, row.revision_number, row.current_url), ('PENDING', 5, SECOND_URL))
            self.assertEqual(_db(lambda: row.revisions.count()), 5)
            self.assertEqual(page.locator('a[href^="http"]').count(), 0)

    def test_phone_manual_validation_save_and_read_only_family_result(self):
        from playwright.sync_api import expect

        client, patient = _patient(get_user_model(), 'cloud-browser-phone')
        viewer_client, viewer_own = _patient(get_user_model(), 'cloud-browser-viewer')
        PatientMembership.objects.create(patient=patient, account=viewer_own.account, role='VIEWER')
        document, original_page, _ = stored_document(patient)
        with self.browser(client, 360) as page:
            page.goto(self.live_server_url + f'/records/{document.pk}/cloud-imaging/?patient={patient.pk}', wait_until='networkidle')
            page.get_by_text('人工补录原页来源', exact=True).click()
            page.get_by_label('来源标题:').fill('人工原页入口')
            page.get_by_label('原页上的完整地址:').fill('http://127.0.0.1/private')
            page.get_by_label('原件页:').select_option(str(original_page.pk))
            page.get_by_role('button', name='保存待核对来源', exact=True).click()
            expect(page.locator('.errorlist')).to_be_visible()
            self.capture(page, 'invalid-manual-phone.png')
            page.get_by_label('原页上的完整地址:').fill(SECOND_URL)
            page.get_by_role('button', name='保存待核对来源', exact=True).click()
            expect(page.get_by_role('heading', name='人工原页入口', exact=True)).to_be_visible()
            page.get_by_text('查看私有来源内容', exact=True).click()
            expect(page.locator('.cloud-payload').first).to_contain_text('SYNTHETIC_CORRECTION')
            self.capture(page, 'manual-source-phone.png')
            source = _db(lambda: CloudImagingSource.objects.get(document=document))
        with self.browser(viewer_client, 360) as viewer:
            viewer.goto(self.live_server_url + f'/cloud-imaging/{source.pk}/', wait_until='networkidle')
            expect(viewer.get_by_role('heading', name='核对结果', exact=True)).to_be_visible()
            self.assertEqual(viewer.get_by_role('button', name='核对并提交', exact=True).count(), 0)
            self.assertEqual(viewer.locator('a[href^="http"]').count(), 0)
            self.capture(viewer, 'read-only-phone.png')
