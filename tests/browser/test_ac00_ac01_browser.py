import importlib.util
import os
import secrets
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from django.conf import settings
from django.contrib.sessions.models import Session
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.crypto import encrypt_phone, hash_phone
from apps.accounts.flow_state import SIGN_IN_PENDING_MFA_SESSION_KEY
from apps.accounts.models import Account, AccountSession, ConsentRecord, OtpChallenge
from apps.accounts.phone import normalize_mainland_phone
from apps.patients.models import Patient
from apps.patients.services import create_patient_space


OTP_CODE = "230412"
FIRST_USE_PASSWORD = "Strong browser passphrase 2026"
RESET_PASSWORD = "Replacement browser passphrase 2026"


def _consume_synthetic_sms_outbox(now):
    """Run the asynchronous delivery step with an explicitly offline provider.

    Playwright's sync API owns an asyncio loop on the browser thread, so run
    Django's synchronous ORM on a separate test worker with its own connection.
    """
    from apps.accounts.providers import DevelopmentSmsProvider
    from apps.accounts.sms_delivery import deliver_sms_job, due_sms_deliveries

    assert settings.DEBUG and settings.OTP_PROVIDER == "development"

    def consume():
        close_old_connections()
        try:
            return [
                deliver_sms_job(job_id, provider=DevelopmentSmsProvider(), now=now)
                for job_id in due_sms_deliveries(now=now)
            ]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(consume).result(timeout=10)


def _browser_executable():
    configured = os.environ.get("PHR_BROWSER_EXECUTABLE")
    candidates = [
        configured,
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    return next((Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)


def _random_phone(prefix="139"):
    return f"{prefix}{secrets.randbelow(100_000_000):08d}"


def _create_password_account(phone, password):
    normalized = normalize_mainland_phone(phone)
    return Account.objects.create_user(
        phone_hash=hash_phone(normalized),
        phone_encrypted=encrypt_phone(normalized),
        password=password,
    )


def _complete_onboarding(account, label):
    return create_patient_space(
        account,
        label,
        {"privacy": True, "sensitive_data": True, "upload_authority": True},
        {"ip": "127.0.0.1", "user_agent": "browser-acceptance"},
    )


def _new_runtime():
    return {"console": [], "page": [], "responses": [], "requests": []}


def _new_page(browser, runtime, viewport=None):
    context = browser.new_context(
        viewport=viewport or {"width": 1440, "height": 900},
        locale="zh-CN",
    )
    page = context.new_page()
    page.set_default_timeout(15_000)
    page.on(
        "console",
        lambda message: runtime["console"].append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: runtime["page"].append(str(error)))
    page.on(
        "response",
        lambda response: runtime["responses"].append(
            (response.status, response.request.method, urlsplit(response.url).path)
        )
        if response.status >= 400
        else None,
    )
    page.on(
        "requestfailed",
        lambda request: runtime["requests"].append(
            (request.method, urlsplit(request.url).path, request.failure)
        ),
    )
    return context, page


def _assert_runtime_clean(test_case, runtime, expected_responses=()):
    test_case.assertEqual(Counter(runtime["responses"]), Counter(expected_responses))
    test_case.assertEqual(runtime["requests"], [])
    test_case.assertEqual(runtime["page"], [])
    expected_console = [
        "Failed to load resource: the server responded with a status of 400 (Bad Request)"
        for status, _method, _path in expected_responses
        if status == 400
    ]
    test_case.assertEqual(Counter(runtime["console"]), Counter(expected_console))


def _assert_no_horizontal_overflow(test_case, page):
    test_case.assertFalse(
        page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
    )


def _browser_session_key(test_case, page):
    cookie = next(
        (item for item in page.context.cookies() if item["name"] == settings.SESSION_COOKIE_NAME),
        None,
    )
    test_case.assertIsNotNone(cookie)
    return cookie["value"]


def _assert_anonymous_browser_session(test_case, page, base_url):
    response = page.context.request.get(f"{base_url}/records/", max_redirects=0)
    test_case.assertEqual(response.status, 302)
    test_case.assertTrue(response.headers["location"].startswith("/login/"))


def _assert_password_visibility(test_case, page, input_id):
    password_input = page.locator(f"#{input_id}")
    toggle = page.locator(f'button[data-password-toggle][aria-controls="{input_id}"]')
    test_case.assertEqual(toggle.count(), 1)
    test_case.assertEqual(toggle.get_attribute("type"), "button")
    test_case.assertEqual(toggle.get_attribute("aria-pressed"), "false")
    test_case.assertEqual(toggle.inner_text(), "显示密码")

    toggle.focus()
    page.keyboard.press("Enter")
    test_case.assertEqual(password_input.get_attribute("type"), "text")
    test_case.assertEqual(toggle.get_attribute("aria-pressed"), "true")
    test_case.assertEqual(toggle.inner_text(), "隐藏密码")

    page.keyboard.press("Space")
    test_case.assertEqual(password_input.get_attribute("type"), "password")
    test_case.assertEqual(toggle.get_attribute("aria-pressed"), "false")
    test_case.assertEqual(toggle.inner_text(), "显示密码")


def _first_use(test_case, page, base_url, phone, code, password, destination="/"):
    query = urlencode({"next": destination})
    response = page.goto(f"{base_url}/login/?{query}", wait_until="networkidle")
    test_case.assertEqual(response.status, 200)
    first_use_link = page.get_by_role("link", name="第一次使用健康之家", exact=True)
    test_case.assertTrue(first_use_link.is_visible())
    test_case.assertTrue(
        page.get_by_role("link", name="忘记密码", exact=True).is_visible()
    )
    first_use_link.click()
    page.wait_for_url(f"{base_url}/login/first-use/**")
    test_case.assertTrue(page.get_by_role("heading", name="第一次使用健康之家").is_visible())
    test_case.assertTrue(page.locator('label[for="id_phone"]').is_visible())
    test_case.assertEqual(page.locator("#id_phone").get_attribute("autocomplete"), "tel")
    page.locator("#id_phone").fill(phone)
    page.get_by_role("button", name="发送验证码", exact=True).click()
    page.wait_for_url(f"{base_url}/login/first-use/verify/")

    test_case.assertTrue(page.locator('label[for="id_code"]').is_visible())
    test_case.assertEqual(page.locator("#id_code").get_attribute("autocomplete"), "one-time-code")
    page.locator("#id_code").fill(code)
    page.get_by_role("button", name="验证手机号", exact=True).click()
    page.wait_for_url(f"{base_url}/login/first-use/password/")

    for input_id, label in (("id_password1", "密码"), ("id_password2", "确认密码")):
        test_case.assertTrue(page.locator(f'label[for="{input_id}"]', has_text=label).is_visible())
        test_case.assertEqual(page.locator(f"#{input_id}").get_attribute("autocomplete"), "new-password")
    _assert_password_visibility(test_case, page, "id_password1")
    page.locator("#id_password1").fill(password)
    page.locator("#id_password2").fill(password)
    page.get_by_role("button", name="设置密码", exact=True).click()


def _start_password_login(test_case, page, base_url, phone, password, destination="/"):
    query = urlencode({"next": destination})
    response = page.goto(f"{base_url}/login/?{query}", wait_until="networkidle")
    test_case.assertEqual(response.status, 200)
    test_case.assertTrue(page.locator('label[for="id_phone"]').is_visible())
    test_case.assertTrue(page.locator('label[for="id_password"]').is_visible())
    test_case.assertEqual(page.locator("#id_phone").get_attribute("autocomplete"), "tel")
    test_case.assertEqual(page.locator("#id_password").get_attribute("autocomplete"), "current-password")
    page.locator("#id_phone").fill(phone)
    page.locator("#id_password").fill(password)
    page.get_by_role("button", name="继续", exact=True).click()
    page.wait_for_url(f"{base_url}/login/verify/")
    test_case.assertTrue(page.locator('label[for="id_code"]').is_visible())
    test_case.assertEqual(page.locator("#id_code").get_attribute("autocomplete"), "one-time-code")
    _assert_anonymous_browser_session(test_case, page, base_url)


def _password_mfa_login(test_case, page, base_url, phone, password, destination="/"):
    _start_password_login(test_case, page, base_url, phone, password, destination)
    page.locator("#id_code").fill(OTP_CODE)
    page.get_by_role("button", name="验证并登录", exact=True).click()
    page.wait_for_url(f"{base_url}{destination}")


def _open_forgot_password(test_case, page, base_url, destination="/"):
    query = urlencode({"next": destination})
    response = page.goto(f"{base_url}/login/?{query}", wait_until="networkidle")
    test_case.assertEqual(response.status, 200)
    forgot_link = page.get_by_role("link", name="忘记密码", exact=True)
    test_case.assertTrue(forgot_link.is_visible())
    forgot_link.click()
    page.wait_for_url(f"{base_url}/login/forgot-password/**")
    forgot_url = urlsplit(page.url)
    test_case.assertEqual(forgot_url.path, "/login/forgot-password/")
    test_case.assertEqual(parse_qs(forgot_url.query).get("next"), [destination])
    test_case.assertTrue(page.locator('label[for="id_phone"]').is_visible())
    test_case.assertEqual(page.locator("#id_phone").get_attribute("autocomplete"), "tel")


@override_settings(
    DEBUG=True,
    OTP_PROVIDER="development",
    OTP_FIXED_CODE=OTP_CODE,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
)
class TestAc00Ac01Browser(StaticLiveServerTestCase):
    def _browser(self):
        if importlib.util.find_spec("playwright") is None:
            self.skipTest("Playwright is not installed")
        executable = _browser_executable()
        if executable is None:
            self.skipTest("No supported local Chromium browser was found")
        from playwright.sync_api import sync_playwright

        return sync_playwright(), executable

    def test_favicon_link_asset_succeeds_and_runtime_observer_records_icon_failures(self):
        playwright_manager, executable = self._browser()
        runtime = _new_runtime()
        with playwright_manager as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context, page = _new_page(browser, runtime, {"width": 390, "height": 844})
            with page.expect_response(
                lambda candidate: urlsplit(candidate.url).path == "/static/favicon.svg"
            ) as favicon_response:
                response = page.goto(f"{self.live_server_url}/login/", wait_until="networkidle")
            self.assertEqual(response.status, 200)
            self.assertEqual(
                page.locator('link[rel~="icon"]').get_attribute("href"),
                "/static/favicon.svg",
            )
            self.assertEqual(favicon_response.value.status, 200)
            self.assertIn("image/svg+xml", favicon_response.value.headers["content-type"])
            _assert_runtime_clean(self, runtime)
            context.close()

            failing_runtime = _new_runtime()
            failing_context, failing_page = _new_page(browser, failing_runtime)
            failing_page.goto(f"{self.live_server_url}/login/", wait_until="networkidle")
            _assert_runtime_clean(self, failing_runtime)
            failing_page.route(
                "**/favicon.ico*",
                lambda route: route.fulfill(
                    status=404,
                    content_type="text/html",
                    body="<main>missing favicon</main>",
                ),
            )
            with failing_page.expect_response(
                lambda candidate: urlsplit(candidate.url).path == "/favicon.ico"
            ) as failed_icon:
                failing_page.evaluate(
                    """new Promise((resolve) => {
                        const image = new Image();
                        image.onload = resolve;
                        image.onerror = resolve;
                        image.src = '/favicon.ico?observer-regression=1';
                        document.body.append(image);
                    })"""
                )
            self.assertEqual(failed_icon.value.status, 404)
            self.assertEqual(failing_runtime["responses"], [(404, "GET", "/favicon.ico")])
            self.assertEqual(failing_runtime["requests"], [])
            self.assertEqual(failing_runtime["page"], [])
            self.assertEqual(
                failing_runtime["console"],
                ["Failed to load resource: the server responded with a status of 404 (Not Found)"],
            )
            failing_context.close()
            browser.close()

    def test_first_use_login_rejections_safe_return_and_auth_viewports(self):
        self.assertEqual(Account.objects.count(), 0)
        login_phone = _random_phone("138")
        login_password = "Normal browser passphrase 2026"
        login_account = _create_password_account(login_phone, login_password)
        login_patient = _complete_onboarding(login_account, "登录状态验收")
        expired_phone = _random_phone("137")
        expired_password = "Expired browser passphrase 2026"
        expired_account = _create_password_account(expired_phone, expired_password)
        _complete_onboarding(expired_account, "过期状态验收")
        expired_client = Client()
        expired_started = expired_client.post(
            "/login/password/",
            {
                "phone": expired_phone,
                "password": expired_password,
                "next": "https://evil.example/",
            },
        )
        self.assertEqual(expired_started.status_code, 302)
        expired_pending = dict(expired_client.session[SIGN_IN_PENDING_MFA_SESSION_KEY])
        self.assertEqual(expired_pending["destination"], "/")
        self.assertNotIn(expired_phone, str(expired_pending))
        self.assertNotIn(expired_password, str(expired_pending))
        self.assertNotIn(OTP_CODE, str(expired_pending))
        expired_pending["issued_at"] -= 300
        expired_session = expired_client.session
        expired_session[SIGN_IN_PENDING_MFA_SESSION_KEY] = expired_pending
        expired_session.save()
        expired_session_key = expired_session.session_key

        playwright_manager, executable = self._browser()
        runtime = _new_runtime()
        with playwright_manager as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context, page = _new_page(browser, runtime)

            for path in ("/login/", "/login/first-use/"):
                for width in (390, 768, 1440):
                    page.set_viewport_size({"width": width, "height": 900})
                    response = page.goto(f"{self.live_server_url}{path}", wait_until="networkidle")
                    self.assertEqual(response.status, 200)
                    _assert_no_horizontal_overflow(self, page)

            first_phone = _random_phone("139")
            first_destination = "/records/?source=browser-first-use"
            page.set_viewport_size({"width": 390, "height": 844})
            _first_use(
                self,
                page,
                self.live_server_url,
                first_phone,
                OTP_CODE,
                FIRST_USE_PASSWORD,
                first_destination,
            )
            page.wait_for_url(f"{self.live_server_url}/onboarding/")
            _assert_no_horizontal_overflow(self, page)

            page.locator("#id_display_name").fill("浏览器验收")
            for field in ("privacy", "sensitive_data", "upload_authority"):
                page.locator(f"#id_{field}").check()
            page.get_by_role("button", name="开始整理", exact=True).click()
            page.wait_for_url(f"{self.live_server_url}{first_destination}")

            login_context, login_page = _new_page(browser, runtime, {"width": 768, "height": 900})
            response = login_page.goto(
                f"{self.live_server_url}/login/?{urlencode({'next': '/records/?source=browser-mfa'})}",
                wait_until="networkidle",
            )
            self.assertEqual(response.status, 200)
            _assert_password_visibility(self, login_page, "id_password")
            login_page.locator("#id_phone").fill(login_phone)
            login_page.locator("#id_password").fill("Wrong browser passphrase")
            with login_page.expect_response(
                lambda candidate: candidate.request.method == "POST"
                and urlsplit(candidate.url).path == "/login/password/"
            ) as rejected_password:
                login_page.get_by_role("button", name="继续", exact=True).click()
            self.assertEqual(rejected_password.value.status, 400)
            self.assertTrue(login_page.get_by_role("alert").is_visible())
            _assert_anonymous_browser_session(self, login_page, self.live_server_url)

            destination = "/records/?source=browser-mfa"
            _password_mfa_login(
                self,
                login_page,
                self.live_server_url,
                login_phone,
                login_password,
                destination,
            )

            missing_context, missing_page = _new_page(browser, runtime, {"width": 390, "height": 844})
            missing = missing_page.goto(f"{self.live_server_url}/login/verify/", wait_until="networkidle")
            self.assertEqual(missing.status, 400)
            self.assertTrue(missing_page.get_by_role("alert").is_visible())
            _assert_anonymous_browser_session(self, missing_page, self.live_server_url)

            expired_context, expired_page = _new_page(browser, runtime, {"width": 1440, "height": 900})
            expired_context.add_cookies(
                [
                    {
                        "name": settings.SESSION_COOKIE_NAME,
                        "value": expired_session_key,
                        "url": self.live_server_url,
                    }
                ]
            )
            expired_mfa = expired_page.goto(
                f"{self.live_server_url}/login/verify/", wait_until="networkidle"
            )
            self.assertEqual(expired_mfa.status, 400)
            self.assertTrue(expired_page.get_by_role("alert").is_visible())
            _assert_anonymous_browser_session(self, expired_page, self.live_server_url)

            _assert_runtime_clean(
                self,
                runtime,
                expected_responses=(
                    (400, "POST", "/login/password/"),
                    (400, "GET", "/login/verify/"),
                    (400, "GET", "/login/verify/"),
                ),
            )
            for browser_context in (context, login_context, missing_context, expired_context):
                browser_context.close()
            browser.close()

        self.assertEqual(Account.objects.count(), 3)
        first_account = Account.objects.exclude(pk__in={login_account.pk, expired_account.pk}).get()
        self.assertTrue(first_account.check_password(FIRST_USE_PASSWORD))
        first_patient = Patient.objects.get(account=first_account)
        self.assertEqual(first_patient.display_name, "浏览器验收")
        self.assertEqual(
            set(ConsentRecord.objects.filter(account=first_account).values_list("consent_type", flat=True)),
            {"privacy", "sensitive_data", "upload_authority"},
        )
        self.assertEqual(Patient.objects.get(pk=login_patient.pk).account_id, login_account.pk)
        self.assertEqual(OtpChallenge.objects.count(), 3)

    def test_legacy_first_use_upgrade_preserves_account_and_patient_ownership(self):
        phone = _random_phone("136")
        normalized = normalize_mainland_phone(phone)
        legacy = Account.objects.create(
            phone_hash=hash_phone(normalized),
            phone_encrypted=encrypt_phone(normalized),
        )
        legacy.set_unusable_password()
        legacy.save(update_fields=["password"])
        patient = _complete_onboarding(legacy, "既有浏览器档案")
        consent_ids = set(ConsentRecord.objects.filter(account=legacy).values_list("pk", flat=True))

        playwright_manager, executable = self._browser()
        runtime = _new_runtime()
        with playwright_manager as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context, page = _new_page(browser, runtime, {"width": 768, "height": 900})
            destination = "/records/?source=legacy-browser"
            _first_use(
                self,
                page,
                self.live_server_url,
                phone,
                OTP_CODE,
                FIRST_USE_PASSWORD,
                destination,
            )
            page.wait_for_url(f"{self.live_server_url}{destination}")

            _assert_runtime_clean(self, runtime)
            context.close()
            browser.close()

        self.assertEqual(Account.objects.count(), 1)
        upgraded = Account.objects.get()
        self.assertEqual(upgraded.pk, legacy.pk)
        self.assertTrue(upgraded.check_password(FIRST_USE_PASSWORD))
        self.assertEqual(Patient.objects.get(pk=patient.pk).account_id, legacy.pk)
        self.assertEqual(
            set(ConsentRecord.objects.filter(account=legacy).values_list("pk", flat=True)),
            consent_ids,
        )

    def test_neutral_password_reset_revokes_browser_and_old_sessions_then_requires_fresh_mfa(self):
        phone = _random_phone("135")
        old_password = "Old browser passphrase 2026"
        reset_destination = "/records/?source=browser-reset"
        account = _create_password_account(phone, old_password)
        _complete_onboarding(account, "重置浏览器验收")
        registered = Client()
        registered.force_login(account)
        registered_key = registered.session.session_key

        playwright_manager, executable = self._browser()
        runtime = _new_runtime()
        with playwright_manager as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            context, page = _new_page(browser, runtime, {"width": 1440, "height": 900})
            _password_mfa_login(self, page, self.live_server_url, phone, old_password)
            current_key = _browser_session_key(self, page)

            missing_context, missing_page = _new_page(browser, runtime, {"width": 390, "height": 844})
            _open_forgot_password(
                self,
                missing_page,
                self.live_server_url,
                reset_destination,
            )
            missing_page.locator("#id_phone").fill(_random_phone("134"))
            missing_page.get_by_role("button", name="发送验证码", exact=True).click()
            missing_page.wait_for_url(
                f"{self.live_server_url}/login/forgot-password/verify/"
            )
            neutral_status = missing_page.get_by_role("status").inner_text()
            self.assertNotEqual(neutral_status, "")

            reset_context, reset_page = _new_page(browser, runtime, {"width": 1440, "height": 900})
            later = timezone.now() + timedelta(seconds=61)
            with patch("apps.accounts.services._now", return_value=later), patch(
                "apps.accounts.authentication.timezone.now", return_value=later
            ):
                _open_forgot_password(
                    self,
                    reset_page,
                    self.live_server_url,
                    reset_destination,
                )
                reset_page.locator("#id_phone").fill(phone)
                reset_page.get_by_role("button", name="发送验证码", exact=True).click()
                reset_page.wait_for_url(
                    f"{self.live_server_url}/login/forgot-password/verify/"
                )
                self.assertEqual(reset_page.get_by_role("status").inner_text(), neutral_status)
                self.assertNotIn(phone, reset_page.locator("body").inner_text())

                self.assertCountEqual(_consume_synthetic_sms_outbox(later), ["discarded", "sent"])

                reset_page.locator("#id_code").fill(OTP_CODE)
                reset_page.get_by_role("button", name="继续", exact=True).click()
                reset_page.wait_for_url(f"{self.live_server_url}/login/forgot-password/new-password/")
                for input_id, label in (("id_password1", "新密码"), ("id_password2", "确认新密码")):
                    self.assertTrue(reset_page.locator(f'label[for="{input_id}"]', has_text=label).is_visible())
                    self.assertEqual(reset_page.locator(f"#{input_id}").get_attribute("autocomplete"), "new-password")
                _assert_password_visibility(self, reset_page, "id_password1")
                reset_page.locator("#id_password1").fill(RESET_PASSWORD)
                reset_page.locator("#id_password2").fill(RESET_PASSWORD)
                reset_page.get_by_role("button", name="完成重置", exact=True).click()
                reset_page.wait_for_url(f"{self.live_server_url}/login/**")
                completed_url = urlsplit(reset_page.url)
                self.assertEqual(completed_url.path, "/login/")
                self.assertEqual(
                    parse_qs(completed_url.query),
                    {
                        "password-reset": ["complete"],
                        "next": [reset_destination],
                    },
                )

            _assert_anonymous_browser_session(self, page, self.live_server_url)

            future = later + timedelta(seconds=61)
            with patch("apps.accounts.services._now", return_value=future):
                reset_page.locator("#id_phone").fill(phone)
                reset_page.locator("#id_password").fill(old_password)
                with reset_page.expect_response(
                    lambda candidate: candidate.request.method == "POST"
                    and urlsplit(candidate.url).path == "/login/password/"
                ) as rejected_old_password:
                    reset_page.get_by_role("button", name="继续", exact=True).click()
                self.assertEqual(rejected_old_password.value.status, 400)
                _assert_anonymous_browser_session(self, reset_page, self.live_server_url)

                _password_mfa_login(
                    self,
                    reset_page,
                    self.live_server_url,
                    phone,
                    RESET_PASSWORD,
                    reset_destination,
                )
                fresh_key = _browser_session_key(self, reset_page)

            _assert_runtime_clean(
                self,
                runtime,
                expected_responses=((400, "POST", "/login/password/"),),
            )
            missing_context.close()
            reset_context.close()
            context.close()
            browser.close()

        account.refresh_from_db()
        self.assertTrue(account.check_password(RESET_PASSWORD))
        self.assertFalse(Session.objects.filter(session_key__in={registered_key, current_key}).exists())
        registered_sessions = list(
            AccountSession.objects.filter(account=account).values_list("session_key", flat=True)
        )
        self.assertEqual(registered_sessions, [fresh_key])
        self.assertNotIn(registered_key, registered_sessions)
        self.assertNotIn(current_key, registered_sessions)
        self.assertTrue(registered.get("/")["Location"].startswith("/login/"))
        self.assertEqual(OtpChallenge.objects.count(), 3)
