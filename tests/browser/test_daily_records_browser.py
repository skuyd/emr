from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.test_detail_viewer import _patient


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestDailyRecordsBrowser(SQLiteSerializedStaticLiveServerTestCase):
    def test_existing_read_failure_retries_and_invalid_save_keeps_input(self):
        from playwright.sync_api import expect, sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'daily-retry-browser')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': 320, 'height': 800})
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                                      'value': client.session.session_key, 'url': self.live_server_url}])
                context.route('**/*', lambda route: route.continue_()
                              if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                page = context.new_page()
                reads = []
                def read_route(route):
                    reads.append(route.request.url)
                    if len(reads) == 1:
                        route.fulfill(status=503, body='unavailable')
                    else:
                        route.continue_()
                page.route('**/self-records/existing/**', read_route)
                page.goto(self.live_server_url + f'/self-records/new/?patient={patient.pk}', wait_until='networkidle')
                expect(page.locator('[data-existing-content]')).to_contain_text('读取失败，请重试')
                page.get_by_role('button', name='重试').click()
                expect(page.locator('[data-existing-content]')).to_contain_text('当天还没有体重记录')
                page.get_by_label('体重', exact=True).fill('bad')
                page.get_by_role('button', name='保存记录').click()
                expect(page.locator('[data-form-error]')).to_contain_text('有限数值')
                self.assertEqual(page.get_by_label('体重', exact=True).input_value(), 'bad')
                self.assertTrue(page.locator('[data-form-notice]').is_hidden())
            finally:
                browser.close()

    def test_ambiguous_local_minute_is_unchanged_across_device_zones_and_keyboard_entry(self):
        from uuid import uuid4

        from playwright.sync_api import expect, sync_playwright

        from apps.self_records.services import create_record

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'daily-zones-browser')
        record = self.database_action(lambda: create_record(patient, patient.account, {
            'kind': 'WEIGHT', 'measured_local': '2026-10-25T02:30', 'value': '60', 'unit': 'kg',
        }, creation_key=uuid4()).record)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                for zone in ('Europe/Berlin', 'Asia/Shanghai'):
                    context = browser.new_context(viewport={'width': 360, 'height': 800}, timezone_id=zone)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                                          'value': client.session.session_key, 'url': self.live_server_url}])
                    context.route('**/*', lambda route: route.continue_()
                                  if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                    page = context.new_page()
                    page.goto(self.live_server_url + f'/self-records/{record.pk}/?patient={patient.pk}',
                              wait_until='networkidle')
                    expect(page.get_by_text('2026-10-25T02:30', exact=True)).to_be_visible()
                    page.get_by_role('link', name='更正记录').click()
                    expect(page.get_by_label('测量时间')).to_have_value('2026-10-25T02:30')
                    self.assertEqual(page.locator('[name="utc_offset"]').count(), 0)
                    context.close()
                context = browser.new_context(viewport={'width': 360, 'height': 800}, timezone_id='Pacific/Honolulu')
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                                      'value': client.session.session_key, 'url': self.live_server_url}])
                page = context.new_page()
                page.goto(self.live_server_url + f'/self-records/?patient={patient.pk}', wait_until='networkidle')
                today = page.evaluate('''() => { const d = new Date(); const p = n => String(n).padStart(2, '0');
                    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; }''')
                self.assertEqual(page.evaluate('new URL(location.href).searchParams.get("date")'), today)
                page.get_by_role('link', name='上个月').click()
                page.get_by_role('link', name='今天', exact=True).click()
                self.assertEqual(page.evaluate('new URL(location.href).searchParams.get("date")'), today)
                page.goto(self.live_server_url + f'/self-records/new/?patient={patient.pk}', wait_until='networkidle')
                ecog = page.get_by_role('button', name='ECOG评分')
                ecog.focus()
                page.keyboard.press('Enter')
                expect(ecog).to_have_attribute('aria-pressed', 'true')
                score = page.locator('input[name="score"][value="5"]')
                score.focus()
                page.keyboard.press('Space')
                self.assertTrue(score.is_checked())
                page.get_by_role('button', name='保存记录').focus()
                page.keyboard.press('Enter')
                expect(page.locator('[data-form-notice]')).to_contain_text('5 分')
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 360)
            finally:
                browser.close()

    def test_desktop_sidebar_edit_restores_draft_and_delete_needs_confirmation(self):
        from playwright.sync_api import expect, sync_playwright
        from uuid import uuid4

        from apps.self_records.services import create_record

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'daily-desktop-browser')
        self.database_action(lambda: create_record(patient, patient.account, {
            'kind': 'WEIGHT', 'measured_local': '2026-10-01T08:30', 'value': '60', 'unit': 'kg',
        }, creation_key=uuid4()))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': 1280, 'height': 900})
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                                      'value': client.session.session_key, 'url': self.live_server_url}])
                context.route('**/*', lambda route: route.continue_()
                              if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                page = context.new_page()
                page.goto(self.live_server_url + f'/self-records/new/?patient={patient.pk}', wait_until='networkidle')
                form = page.locator('[data-record-form]').bounding_box()
                side = page.locator('[data-existing-panel]').bounding_box()
                self.assertLess(form['x'] + form['width'], side['x'])
                self.assertLess(abs(form['y'] - side['y']), 8)
                page.get_by_label('测量时间').fill('2026-10-01T08:30')
                expect(page.locator('[data-existing-content]')).to_contain_text('60 kg')
                page.get_by_label('体重', exact=True).fill('70')
                page.get_by_label('单位').select_option('lb')
                page.get_by_role('button', name='体温').click()
                page.get_by_label('体温', exact=True).fill('37')
                page.get_by_role('button', name='体重').click()
                self.assertEqual(page.get_by_label('体重', exact=True).input_value(), '70')
                self.assertEqual(page.get_by_label('单位').input_value(), 'lb')
                page.locator('[data-existing-content]').get_by_role('button', name='更正').click()
                expect(page.get_by_role('heading', name='更正体重记录')).to_be_visible()
                page.get_by_label('体重', exact=True).fill('61')
                page.get_by_role('button', name='保存更正').click()
                expect(page.get_by_role('heading', name='记一条')).to_be_visible()
                self.assertEqual(page.get_by_label('体重', exact=True).input_value(), '70')
                self.assertEqual(page.get_by_label('单位').input_value(), 'lb')
                expect(page.locator('[data-existing-content]')).to_contain_text('61 kg')
                page.locator('[data-existing-content]').get_by_role('button', name='删除').click()
                expect(page.get_by_text('确认删除这条体重记录？')).to_be_visible()
                page.get_by_role('button', name='取消', exact=True).focus()
                page.keyboard.press('Enter')
                expect(page.locator('[data-existing-content]')).to_contain_text('61 kg')
                page.locator('[data-existing-content]').get_by_role('button', name='删除').click()
                page.get_by_label('测量时间').fill('2026-10-02T08:30')
                expect(page.get_by_role('button', name='确认删除')).to_have_count(0)
                page.get_by_label('测量时间').fill('2026-10-01T08:30')
                expect(page.locator('[data-existing-content]')).to_contain_text('61 kg')
                page.locator('[data-existing-content]').get_by_role('button', name='更正').click()
                expect(page.get_by_role('heading', name='更正体重记录')).to_be_visible()
                page.locator('[data-existing-content]').get_by_role('button', name='删除').click()
                page.get_by_role('button', name='确认删除').click()
                expect(page.get_by_role('heading', name='记一条')).to_be_visible()
                expect(page.locator('[data-existing-content]')).to_contain_text('当天还没有体重记录')
                expect(page.locator('[data-form-notice]')).to_contain_text('已删除记录')
                self.assertEqual(page.get_by_label('体重', exact=True).input_value(), '70')
                self.assertEqual(page.locator('text=撤销').count(), 0)
            finally:
                browser.close()

    def test_continuous_entry_sidebar_edit_and_confirmed_delete_at_phone_width(self):
        from playwright.sync_api import expect, sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'daily-new-browser')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': 320, 'height': 800}, timezone_id='Europe/Berlin')
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                                      'value': client.session.session_key, 'url': self.live_server_url}])
                context.route('**/*', lambda route: route.continue_()
                              if route.request.url.startswith(self.live_server_url + '/') else route.abort())
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(self.live_server_url + f'/self-records/?patient={patient.pk}', wait_until='networkidle')
                expect(page.get_by_role('link', name='日历')).to_have_attribute('aria-current', 'page')
                page.get_by_role('link', name='列表', exact=True).click()
                page.get_by_role('link', name='记一条', exact=True).click()
                expect(page.get_by_text('当天还没有体重记录')).to_be_visible()
                page.get_by_label('测量时间').fill('2026-10-25T02:30')
                page.get_by_label('体重', exact=True).fill('60')
                page.get_by_role('button', name='保存记录').click()
                expect(page.get_by_text('可继续添加下一条', exact=False)).to_be_visible()
                expect(page.locator('[data-form-notice]')).to_contain_text('2026-10-25 02:30')
                self.assertNotEqual(page.get_by_label('测量时间').input_value(), '2026-10-25T02:30')
                page.get_by_label('测量时间').fill('2026-10-25T02:30')
                expect(page.locator('[data-existing-content]')).to_contain_text('60 kg')
                page.get_by_role('button', name='ECOG评分').click()
                expect(page.get_by_label('日期', exact=True)).to_be_visible()
                page.get_by_label('日期', exact=True).fill('2026-10-25')
                page.locator('input[name="score"][value="0"]').check()
                page.get_by_role('button', name='保存记录').click()
                expect(page.locator('[data-form-notice]')).to_contain_text('2026-10-25')
                page.get_by_label('日期', exact=True).fill('2026-10-25')
                expect(page.locator('[data-existing-content]')).to_contain_text('0 分')
                self.assertEqual(page.evaluate('document.documentElement.scrollWidth <= innerWidth'), True)
                page.get_by_role('link', name='返回列表').click()
                expect(page.get_by_role('link', name='列表', exact=True)).to_have_attribute('aria-current', 'page')
                expect(page.get_by_text('0 分', exact=False).first).to_be_visible()
                self.assertEqual(errors, [])
            finally:
                browser.close()
