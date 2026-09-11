"""Actual TLS phone read keeps all four selected domains and clears on invalidation."""
import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.patients.sharing import create_share
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_cloud_open_browser as tls
from tests.exports.test_molecular_combined_domains import four_domains,change_domain
from tests.cloud_imaging.test_source_services import FIRST_URL
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_shares import exchange


@override_settings(DEBUG=True,SESSION_COOKIE_SECURE=True,CSRF_COOKIE_SECURE=True)
class TestMolecularCombinedBrowser(SQLiteSerializedStaticLiveServerTestCase):
    server_thread_class=tls.TLSLiveServerThread
    tls_url=tls.TestCloudOpenBrowser.tls_url
    browser=tls.TestCloudOpenBrowser.browser

    def test_phone_four_domain_share_clears_after_molecular_change(self):
        from playwright.sync_api import expect
        patient,_,lesion,cloud,scope,metric,_=four_domains(get_user_model())
        made=create_share(patient,patient.account,scope)
        reader,_=_patient(get_user_model(),"four-domain-phone")
        identity=exchange(reader,made.token)
        with self.browser(reader,360) as (_,page,external,posts):
            status=self.tls_url+f"/shared/{identity}/status/"
            with page.expect_response(lambda response:response.url==status) as initial:
                response=page.goto(self.tls_url+f"/shared/{identity}/",wait_until="domcontentloaded")
            self.assertEqual(response.status,200);self.assertEqual(initial.value.status,200)
            for original in ("01.20","NM_SYN.2","肺癌","人工确认的观察分组","=选定观察 <A>"):
                expect(page.locator("main")).to_contain_text(original)
            expect(page.get_by_role("heading",name="选定云影像来源",exact=True)).to_be_visible()
            self.assertNotIn(FIRST_URL,page.content())
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),360)
            folder=os.environ.get("PHR_MOLECULAR_COMBINED_BROWSER_ARTIFACT_DIR")
            if folder:
                target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(target/"four-domain-share-360.png"),full_page=True)
            self.database_action(lambda:change_domain(patient,lesion,cloud,metric,"molecular"))
            with page.expect_response(lambda response:response.url==status) as changed:
                page.evaluate("window.dispatchEvent(new Event('pageshow'))")
            self.assertEqual(changed.value.status,410)
            expect(page.get_by_role("alert")).to_contain_text("分享已失效")
            self.assertNotIn("NM_SYN.2",page.locator("main").inner_text())
            self.assertNotIn("肺癌",page.locator("main").inner_text())
            self.assertEqual(page.locator('a[href*="cloud-imaging"]').count(),0)
            self.assertEqual(external,[]);self.assertEqual(posts,[])
        made.share.refresh_from_db()
        self.assertEqual(made.share.snapshot,{})
        self.assertFalse(made.share.cloud_sources.exists())
