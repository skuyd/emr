from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import override_settings

from apps.accounts.crypto import encrypt_phone, hash_phone
from apps.patients.models import PatientMembership
from tests.browser.test_ac00_ac01_browser import OTP_CODE
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.documents.test_detail_viewer import _document, _patient
from tests.documents.test_detail_viewer import _png_bytes
from tests.documents.fakes import InMemoryObjectStore
from tests.patients.test_family_invitations import PHONE, recipient


@override_settings(DEBUG=True, OTP_PROVIDER="development", OTP_FIXED_CODE=OTP_CODE,
                   SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestFamilySharingBrowser(SQLiteSerializedStaticLiveServerTestCase):
    def test_invitation_survives_real_login_and_clears_browser_token(self):
        from playwright.sync_api import expect, sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        owner_client, patient = _patient(get_user_model(), "browser-inviter")
        _document(patient)
        _, target = recipient(get_user_model(), "browser-invite-target")
        target.phone_encrypted = encrypt_phone(PHONE)
        target.set_password("Local family browser passphrase 2026")
        target.save(update_fields=["phone_encrypted", "password"])
        observed, errors = [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            owner_context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
            owner_context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": owner_client.session.session_key, "url": self.live_server_url}])
            target_context = browser.new_context(viewport={"width": 360, "height": 780}, locale="zh-CN")
            for context in (owner_context, target_context):
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                context.on("request", lambda request: observed.append((request.url, request.headers.get("referer", ""))))
            owner = owner_context.new_page()
            owner.on("pageerror", lambda error: errors.append(str(error)))
            owner.goto(self.live_server_url + f"/patients/{patient.pk}/members/", wait_until="networkidle")
            owner.get_by_role("link", name="邀请家庭成员", exact=True).click()
            owner.get_by_label("接收者已验证手机号").fill(PHONE)
            owner.get_by_label("访问角色").select_option("VIEWER")
            owner.get_by_label("成员称呼（可选）").fill("浏览器家人")
            owner.get_by_role("button", name="生成邀请链接", exact=True).click()
            link = owner.get_by_label("邀请链接", exact=True).input_value()
            token = parse_qs(urlsplit(link).fragment)["token"][0]
            page = target_context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(link, wait_until="networkidle")
            expect(page.get_by_role("link", name="登录后继续", exact=True)).to_be_visible()
            self.assertEqual(page.url, self.live_server_url + "/family/invitation/")
            self.assertTrue(page.evaluate("sessionStorage.getItem('phr:pending-invitation') !== null"))
            page.get_by_role("link", name="登录后继续", exact=True).click()
            page.get_by_label("手机号", exact=True).fill(PHONE)
            page.get_by_label("密码", exact=True).fill("Local family browser passphrase 2026")
            page.get_by_role("button", name="继续", exact=True).click()
            page.wait_for_url(self.live_server_url + "/login/verify/")
            page.locator("#id_code").fill(OTP_CODE)
            page.get_by_role("button", name="验证并登录", exact=True).click()
            page.wait_for_url(self.live_server_url + "/family/invitation/")
            expect(page.get_by_role("button", name="接受邀请", exact=True)).to_be_visible()
            page.get_by_role("button", name="接受邀请", exact=True).click()
            page.wait_for_url(self.live_server_url + f"/records/?patient={patient.pk}")
            self.assertIsNone(page.evaluate("sessionStorage.getItem('phr:pending-invitation')"))
            denied = page.evaluate("""async (patient) => {
              const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
              return (await fetch('/me/name/', {method: 'POST', headers: {'X-CSRFToken': csrf},
                body: new URLSearchParams({patient_id: patient, display_name: 'forged'})})).status;
            }""", str(patient.pk))
            self.assertEqual(denied, 403)
            self.assertEqual(errors, [])
            self.assertTrue(all(token not in url and token not in referer for url, referer in observed))
            box = page.get_by_role("link", name="切换患者", exact=True).bounding_box()
            self.assertGreaterEqual(box["x"], 0)
            self.assertLessEqual(box["x"] + box["width"], 360)
            browser.close()
        self.assertEqual(PatientMembership.objects.get(patient=patient, account=target).role, "VIEWER")
        self.assertTrue(all(token not in repr(session.get_decoded()) for session in Session.objects.all()))
        patient.refresh_from_db()
        self.assertEqual(patient.display_name, "测试患者")

    def test_desktop_share_mobile_login_source_download_revocation_and_audit(self):
        from pathlib import Path
        from unittest.mock import patch
        from playwright.sync_api import expect, sync_playwright
        from apps.operations.audit import _hash
        from apps.operations.models import AuditEvent

        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        owner_client, patient = _patient(get_user_model(), "browser-share-owner")
        chosen, _ = _document(patient, content_type="image/png", page_count=1)
        other, _ = _document(patient)
        _, target = recipient(get_user_model(), "browser-share-target")
        share_phone = "+8613900000022"
        target.phone_encrypted = encrypt_phone(share_phone)
        target.phone_hash = hash_phone(share_phone)
        target.set_password("Local sharing browser passphrase 2026")
        target.save(update_fields=["phone_encrypted", "phone_hash", "password"])
        store = InMemoryObjectStore()
        payload = _png_bytes()
        store.objects[chosen.original_object_key] = payload
        observed, errors = [], []
        with patch("apps.patients.share_views.get_object_store", return_value=store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            owner_context = browser.new_context(viewport={"width": 1280, "height": 800}, locale="zh-CN")
            owner_context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": owner_client.session.session_key, "url": self.live_server_url}])
            reader_context = browser.new_context(viewport={"width": 360, "height": 780}, locale="zh-CN", accept_downloads=True)
            for context in (owner_context, reader_context):
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.live_server_url + "/") else route.abort())
                context.on("request", lambda request: observed.append((request.url, request.headers.get("referer", ""))))
            owner = owner_context.new_page()
            owner.on("pageerror", lambda error: errors.append(str(error)))
            owner.goto(self.live_server_url + f"/patients/{patient.pk}/shares/", wait_until="networkidle")
            owner.get_by_label(chosen.display_filename, exact=True).check()
            owner.get_by_label("原件来源（完整选定文件）", exact=True).check()
            owner.get_by_label("允许下载完整原件").check()
            owner.get_by_role("button", name="生成分享链接", exact=True).click()
            link = owner.get_by_label("分享链接", exact=True).input_value()
            token = parse_qs(urlsplit(link).fragment)["token"][0]
            page = reader_context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(link, wait_until="networkidle")
            expect(page.get_by_role("link", name="登录后继续", exact=True)).to_be_visible()
            self.assertEqual(page.url, self.live_server_url + "/shared/open/")
            page.get_by_role("link", name="登录后继续", exact=True).click()
            page.get_by_label("手机号", exact=True).fill(share_phone)
            page.get_by_label("密码", exact=True).fill("Local sharing browser passphrase 2026")
            page.get_by_role("button", name="继续", exact=True).click()
            page.wait_for_url(self.live_server_url + "/login/verify/")
            page.locator("#id_code").fill(OTP_CODE)
            page.get_by_role("button", name="验证并登录", exact=True).click()
            expect(page.get_by_role("heading", name="只读资料分享", exact=True)).to_be_visible()
            self.assertIsNone(page.evaluate("sessionStorage.getItem('phr:pending-share')"))
            self.assertNotIn(other.display_filename, page.locator("main").inner_text())
            self.assertEqual(page.evaluate("async (url) => (await fetch(url)).status", f"/records/{chosen.pk}/"), 404)
            original_url = page.get_by_role("link", name="下载原件", exact=True).get_attribute("href")
            with page.expect_download() as download_event:
                page.get_by_role("link", name="下载原件", exact=True).click()
            self.assertEqual(Path(download_event.value.path()).read_bytes(), payload)
            page.get_by_role("link", name="查看这份原件", exact=True).click()
            image = page.locator("[data-share-content] img").first
            expect(image).to_be_visible()
            page.wait_for_function("() => document.querySelector('[data-share-content] img').naturalWidth > 0")
            self.assertLessEqual(image.bounding_box()["width"], 360)
            owner.get_by_role("button", name="撤销分享", exact=True).click()
            page.evaluate("window.dispatchEvent(new Event('pageshow'))")
            expect(page.get_by_role("alert")).to_contain_text("分享已失效")
            self.assertEqual(page.evaluate("async (url) => (await fetch(url)).status", original_url), 410)
            owner.goto(self.live_server_url + f"/patients/{patient.pk}/audit/", wait_until="networkidle")
            owner.get_by_label("操作", exact=False).first.select_option("original_downloaded")
            owner.get_by_label("结果", exact=False).select_option("denied")
            owner.get_by_role("button", name="筛选记录", exact=True).click()
            expect(owner.locator("tbody")).to_contain_text("已拒绝")
            self.assertTrue(all(token not in url and token not in referer for url, referer in observed))
            self.assertEqual(errors, [])
            browser.close()
        self.assertTrue(all(token not in repr(session.get_decoded()) for session in Session.objects.all()))
        self.assertFalse(PatientMembership.objects.filter(patient=patient, account=target).exists())
        self.assertTrue(AuditEvent.objects.filter(actor_hash=_hash("actor", target.pk), action="original_downloaded", result="succeeded").exists())
