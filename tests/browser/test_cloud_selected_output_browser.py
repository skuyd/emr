"""Actual TLS selected-source forms and recipient-only controlled navigation."""
from urllib.parse import parse_qs, urlsplit, urldefrag

from django.contrib.auth import get_user_model
from django.test import override_settings

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_cloud_open_browser as browser_support
from tests.browser.test_phase_three_browser import _db
from tests.cloud_imaging.test_controlled_open import confirmed
from tests.cloud_imaging.test_source_services import FIRST_URL, SECOND_URL, _decide
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_shares import exchange


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
class TestCloudSelectedOutputBrowser(SQLiteSerializedStaticLiveServerTestCase):
    server_thread_class = browser_support.TLSLiveServerThread
    tls_url = browser_support.TestCloudOpenBrowser.tls_url
    browser = browser_support.TestCloudOpenBrowser.browser
    capture = browser_support.TestCloudOpenBrowser.capture

    def exercise(self, width):
        from playwright.sync_api import expect
        from apps.exports.models import ExportJob
        from apps.patients.models import PatientShare

        owner, patient, document, source = confirmed(get_user_model())
        reader, _ = _patient(get_user_model(), 'cloud-output-browser-reader')
        with self.browser(owner, width) as (_, page, external, posts):
            page.goto(self.tls_url + '/visit/', wait_until='networkidle')
            page.locator('[name="mode"]').select_option('documents')
            page.locator('[name="cloud_source_ids"]').check()
            page.get_by_role('button', name='预览内容与导出清单', exact=True).click()
            expect(page).to_have_url(__import__('re').compile('/visit/[0-9a-f-]+/$'))
            job = _db(lambda: ExportJob.objects.get(patient=patient))
            self.assertEqual(job.snapshot['documents'], [])
            self.assertEqual(job.snapshot['selection']['cloud_source_ids'], [str(source.pk)])
            self.assertIn(FIRST_URL, page.content())
            self.assertEqual(external, [])
            self.capture(page, f'selected-preview-{width}.png')

            page.goto(self.tls_url + f'/patients/{patient.pk}/shares/', wait_until='networkidle')
            for box in page.locator('[name="sections"]').all():
                box.uncheck()
            page.locator('[name="cloud_source_ids"]').check()
            page.locator('form').filter(has=page.locator('[name="cloud_source_ids"]')).locator('button[type="submit"]').click()
            link = page.locator('input[name="share_link"]')
            expect(link).to_be_visible()
            token = parse_qs(urlsplit(link.input_value()).fragment)['token'][0]
            share = _db(lambda: PatientShare.objects.get(patient=patient))
            self.assertEqual(share.scope['document_ids'], [])
            self.assertEqual(_db(lambda: share.source_bindings.count()), 0)
            self.assertNotIn('SYNTHETIC_FIRST', page.content())
            self.assertEqual(external, [])
            self.assertEqual(posts, [])
        share_id = exchange(reader, token)
        with self.browser(reader, width) as (context, page, external, posts):
            page.goto(self.tls_url + f'/shared/{share_id}/', wait_until='domcontentloaded')
            expect(page.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
            self.assertNotIn('SYNTHETIC_FIRST', page.content())
            self.assertEqual(page.locator(f'a[href*="/documents/{document.pk}/"]').count(), 0)
            page.locator(f'a[href$="/cloud-imaging/{source.pk}/visit/"]').click()
            expect(page.get_by_role('heading', name='打开云影像外部站点', exact=True)).to_be_visible()
            self.assertNotIn('SYNTHETIC_FIRST', page.content())
            self.assertEqual(external, [])
            self.capture(page, f'shared-notice-{width}.png')
            with context.expect_page() as created:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            popup = created.value
            expect(popup.get_by_text('Synthetic external destination', exact=True)).to_be_visible()
            self.assertEqual(external[0]['url'], urldefrag(FIRST_URL)[0])
            self.assertNotIn('referer', external[0]['headers'])
            self.assertIsNone(popup.evaluate('window.opener'))
            self.assertEqual(popup.evaluate('document.referrer'), '')
            self.assertEqual(posts[0]['headers'].get('origin'), self.tls_url)
            self.assertNotIn('referer', posts[0]['headers'])
            popup.close()
            _db(lambda: _decide(patient, source, 'CORRECT', changes={'url':SECOND_URL}))
            with context.expect_page() as stale:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            expect(page.locator('[data-cloud-open-error]')).to_be_visible()
            self.assertTrue(stale.value.is_closed())
            self.assertEqual(len(external), 1)
            self.capture(page, f'shared-stale-{width}.png')

    def test_desktop_source_only_preview_share_and_controlled_external_open(self):
        self.exercise(1280)

    def test_phone_source_only_preview_share_and_controlled_external_open(self):
        self.exercise(360)
