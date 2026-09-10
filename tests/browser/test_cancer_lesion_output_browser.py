"""Combined selected meanings remain distinct on the actual shared phone page."""
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.lesions.services import rename_lesion
from apps.patients.sharing import create_share
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_cancer_ordering_browser import TestCancerOrderingBrowser as Harness
from tests.cancer_ordering.test_lesion_output_compatibility import mixed_selected
from tests.cloud_imaging.test_source_services import FIRST_URL
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_shares import exchange


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestCancerLesionSharedBrowser(SQLiteSerializedStaticLiveServerTestCase):
    browser = Harness.browser
    capture = Harness.capture

    def test_combined_selected_content_and_lesion_change_clear_phone_body(self):
        from playwright.sync_api import expect

        patient, _, lesion, _, scope = mixed_selected(get_user_model())
        made = create_share(patient, patient.account, scope)
        self.assertTrue(all(made.share.snapshot[key] for key in
                            ('cancer_candidates', 'indicator_ordering', 'lesions', 'cloud_imaging_sources')))
        reader, _ = _patient(get_user_model(), 'mixed-phone-reader')
        identity = exchange(reader, made.token)
        with self.browser(reader, 360) as page:
            page.goto(self.live_server_url + f'/shared/{identity}/', wait_until='networkidle')
            expect(page.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
            expect(page.locator('main')).to_contain_text('人工确认的观察分组')
            expect(page.locator('main')).to_contain_text('=选定观察 <A>')
            expect(page.locator('main')).to_contain_text('肺癌')
            expect(page.get_by_role('heading', name='选定云影像来源', exact=True)).to_be_visible()
            self.assertNotIn(FIRST_URL, page.content())
            self.assertEqual(page.locator('a[href*="/lesions/"]').count(), 0)
            self.capture(page, 'combined-cancer-lesion-cloud-share-360.png')
            self.database_action(lambda: rename_lesion(patient, actor=patient.account, lesion_id=lesion.pk,
                                 expected_revision=lesion.revision_number, name='SYNTHETIC_CHANGED_LESION'))
            page.evaluate("window.dispatchEvent(new Event('pageshow'))")
            expect(page.get_by_role('alert')).to_contain_text('分享已失效')
            self.assertNotIn('=选定观察 <A>', page.locator('main').inner_text())
            self.assertNotIn('肺癌', page.locator('main').inner_text())
            self.assertEqual(page.locator('a[href*="cloud-imaging"]').count(), 0)
        made.share.refresh_from_db()
        self.assertEqual(made.share.snapshot, {})
        self.assertFalse(made.share.cloud_sources.exists())
