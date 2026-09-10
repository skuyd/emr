"""Actual TLS, native browser POST and a completely intercepted synthetic site."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import ssl
import tempfile
from urllib.parse import urldefrag

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.servers.basehttp import WSGIRequestHandler
from django.test import override_settings
from django.test.testcases import LiveServerThread

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase, SQLiteSerializedWSGIServer
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.cloud_imaging.test_controlled_open import confirmed
from tests.cloud_imaging.test_source_services import FIRST_URL, SECOND_URL, _decide


class TLSHandler(WSGIRequestHandler):
    def get_environ(self):
        result = super().get_environ()
        # This socket is genuinely TLS; no forwarded/synthetic request header.
        result['HTTPS'] = 'on'
        return result

    def log_message(self, *args):
        pass


class TLSLiveServerThread(LiveServerThread):
    server_class = SQLiteSerializedWSGIServer

    def _create_server(self, connections_override=None):
        server = self.server_class((self.host, self.port), TLSHandler,
                                  allow_reuse_address=False, connections_override=connections_override)
        with tempfile.TemporaryDirectory(prefix='cloud-open-tls-') as folder:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
            now = datetime.now(timezone.utc)
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                    x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(key, hashes.SHA256()))
            cert_path, key_path = Path(folder) / 'cert.pem', Path(folder) / 'key.pem'
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        return server


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
class TestCloudOpenBrowser(SQLiteSerializedStaticLiveServerTestCase):
    server_thread_class = TLSLiveServerThread

    @property
    def tls_url(self):
        return self.live_server_url.replace('http:', 'https:', 1)

    @contextmanager
    def browser(self, client, width, *, javascript=True):
        from playwright.sync_api import sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': width, 'height': 844},
                    locale='zh-CN', ignore_https_errors=True, java_script_enabled=javascript)
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                    'value': client.session.session_key, 'url': self.tls_url, 'secure': True}])
                external, posts, errors = [], [], []
                def route_request(route):
                    request = route.request
                    if request.url.startswith(self.tls_url + '/'):
                        if '/open/' in request.url:
                            posts.append({'url': request.url, 'method': request.method,
                                          'headers': request.all_headers()})
                        route.continue_()
                    elif request.url.startswith(('https://images.example.invalid/', 'https://xn--fa-hia.example.invalid/')):
                        external.append({'url': request.url, 'headers': request.all_headers()})
                        route.fulfill(status=200, content_type='text/html',
                                      body='<title>Synthetic site</title><p>Synthetic external destination</p>')
                    else:
                        errors.append('unexpected_external_request')
                        route.abort()
                context.route('**/*', route_request)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                yield context, page, external, posts
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def capture(self, page, filename):
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), page.viewport_size['width'])
        folder = os.environ.get('PHR_CLOUD_OPEN_BROWSER_ARTIFACT_DIR')
        if folder:
            directory = Path(folder)
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / filename), full_page=True)

    def exercise(self, width):
        from playwright.sync_api import expect

        client, patient, _, source = confirmed(get_user_model())
        with self.browser(client, width) as (context, page, external, posts):
            page.goto(self.tls_url + f'/cloud-imaging/{source.pk}/', wait_until='networkidle')
            page.get_by_role('link', name='查看外部访问说明', exact=True).click()
            expect(page.get_by_role('heading', name='访问说明', exact=True)).to_be_visible()
            self.assertEqual(external, [])
            self.assertNotIn('SYNTHETIC_FIRST', page.content())
            self.capture(page, f'notice-{width}.png')
            with context.expect_page() as created:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            popup = created.value
            expect(popup.get_by_text('Synthetic external destination', exact=True)).to_be_visible()
            self.assertEqual(len(external), 1)
            self.assertEqual(external[0]['url'], urldefrag(FIRST_URL)[0])
            self.assertNotIn('referer', external[0]['headers'])
            self.assertIsNone(popup.evaluate('window.opener'))
            self.assertEqual(popup.evaluate('document.referrer'), '')
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]['method'], 'POST')
            self.assertEqual(posts[0]['headers'].get('origin'), self.tls_url)
            self.assertNotIn('referer', posts[0]['headers'])
            popup.close()

            # Keep the old notice alive while a real source correction commits.
            _db(lambda: _decide(patient, source, 'CORRECT', changes={'url': SECOND_URL}))
            with context.expect_page() as stale_page:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            stale = stale_page.value
            expect(page.locator('[data-cloud-open-error]')).to_be_visible()
            if not stale.is_closed():
                stale.wait_for_event('close')
            self.assertTrue(stale.is_closed())
            self.assertEqual(len(external), 1)
            self.capture(page, f'stale-{width}.png')
            page.get_by_role('link', name='返回来源核对', exact=True).click()
            expect(page.get_by_role('heading', name='核对结果', exact=True)).to_be_visible()
            page.get_by_role('link', name='查看外部访问说明', exact=True).click()
            with context.expect_page() as corrected:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            last = corrected.value
            expect(last.get_by_text('Synthetic external destination', exact=True)).to_be_visible()
            self.assertEqual(external[-1]['url'], urldefrag(SECOND_URL)[0])
            self.assertIsNone(last.evaluate('window.opener'))
            self.assertNotIn('referer', external[-1]['headers'])

    def test_desktop_tls_explicit_open_and_source_change_recovery(self):
        self.exercise(1280)

    def test_phone_tls_explicit_open_and_source_change_recovery(self):
        self.exercise(360)

    def test_network_failure_closes_only_the_empty_window_and_keeps_recovery(self):
        from playwright.sync_api import expect

        client, _, _, source = confirmed(get_user_model())
        with self.browser(client, 360) as (context, page, external, _):
            page.goto(self.tls_url + f'/cloud-imaging/{source.pk}/visit/', wait_until='networkidle')
            context.route('**/open/**', lambda route: route.abort())
            with context.expect_page() as created:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            empty = created.value
            expect(page.locator('[data-cloud-open-error]')).to_be_visible()
            if not empty.is_closed():
                empty.wait_for_event('close')
            self.assertTrue(empty.is_closed())
            self.assertEqual(external, [])
            self.assertFalse(page.is_closed())
            expect(page.get_by_role('link', name='返回来源核对', exact=True)).to_be_visible()

    def test_without_javascript_explains_the_disabled_action_without_any_external_request(self):
        from playwright.sync_api import expect

        client, _, _, source = confirmed(get_user_model())
        with self.browser(client, 360, javascript=False) as (_, page, external, posts):
            page.goto(self.tls_url + f'/cloud-imaging/{source.pk}/visit/', wait_until='networkidle')
            expect(page.get_by_role('button', name='确认并在新窗口打开', exact=True)).to_be_disabled()
            expect(page.get_by_text('请启用 JavaScript 后主动打开，或返回来源核对页面查看原页。', exact=True)).to_be_visible()
            page.get_by_role('link', name='返回来源核对', exact=True).click()
            expect(page.get_by_role('heading', name='核对结果', exact=True)).to_be_visible()
            self.assertEqual(external, [])
            self.assertEqual(posts, [])

    def test_unicode_path_duplicate_parameters_and_fragment_reach_only_the_browser_destination(self):
        from playwright.sync_api import expect

        client, patient, _, source = confirmed(get_user_model())
        raw = 'https://images.example.invalid/影像/a%2fb?next=https://other.invalid/&x=1&x=SECOND%2Bv#access-token'
        source = _decide(patient, source, 'CORRECT', changes={'url': raw})
        with self.browser(client, 1280) as (context, page, external, _):
            page.goto(self.tls_url + f'/cloud-imaging/{source.pk}/visit/', wait_until='networkidle')
            self.assertNotIn('access-token', page.content())
            with context.expect_page() as created:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            destination = created.value
            expect(destination.get_by_text('Synthetic external destination', exact=True)).to_be_visible()
            expected = 'https://images.example.invalid/%E5%BD%B1%E5%83%8F/a%2fb?next=https://other.invalid/&x=1&x=SECOND%2Bv'
            self.assertEqual(external[0]['url'], expected)
            self.assertEqual(destination.url, expected + '#access-token')
            self.assertNotIn('referer', external[0]['headers'])
            self.assertIsNone(destination.evaluate('window.opener'))

    def test_displayed_idna_site_is_the_actual_browser_host(self):
        from playwright.sync_api import expect

        client, patient, _, source = confirmed(get_user_model())
        source = _decide(patient, source, 'CORRECT', changes={'url': 'https://faß.example.invalid/view?key=UNCHANGED'})
        with self.browser(client, 360) as (context, page, external, _):
            page.goto(self.tls_url + f'/cloud-imaging/{source.pk}/visit/', wait_until='networkidle')
            expect(page.locator('code')).to_have_text('xn--fa-hia.example.invalid')
            with context.expect_page() as created:
                page.get_by_role('button', name='确认并在新窗口打开', exact=True).click()
            destination = created.value
            expect(destination.get_by_text('Synthetic external destination', exact=True)).to_be_visible()
            self.assertEqual(external[0]['url'], 'https://xn--fa-hia.example.invalid/view?key=UNCHANGED')
            self.assertNotIn('referer', external[0]['headers'])
            self.assertIsNone(destination.evaluate('window.opener'))
