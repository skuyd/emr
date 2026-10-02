import os
import re
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.processing.models import DatePrecision, DocumentType
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.test_records import _patient, _record


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestRecordsCalendarBrowser(SQLiteSerializedStaticLiveServerTestCase):
    def test_calendar_selects_day_and_keeps_uncertain_dates_separate(self):
        from playwright.sync_api import expect, sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'records-calendar-browser')
        first = self.database_action(lambda: _record(
            patient, 'october-second.pdf', document_date=date(2026, 10, 2),
            precision=DatePrecision.DAY, document_type=DocumentType.LAB,
        ))
        self.database_action(lambda: _record(
            patient, 'october-third.pdf', document_date=date(2026, 10, 3),
            precision=DatePrecision.DAY, document_type=DocumentType.LAB,
        ))
        self.database_action(lambda: _record(patient, 'uncertain.pdf', document_type=DocumentType.LAB))
        artifacts = os.environ.get('PHR_RECORDS_CALENDAR_ARTIFACT_DIR')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='zh-CN')
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                                     'value': client.session.session_key, 'url': self.live_server_url}])
                page = context.new_page()
                page.goto(self.live_server_url + f'/records/?patient={patient.pk}&year=2026&month=10&date=2026-10-02',
                          wait_until='networkidle')
                expect(page.get_by_role('link', name='日历', exact=True)).to_have_attribute('aria-current', 'page')
                expect(page.get_by_role('table', name='2026 年 10 月资料日历')).to_be_visible()
                expect(page.locator('.records-calendar-day')).to_have_count(35)
                expect(page.get_by_role('link', name=re.compile('^2026-10-02，1 份资料'))).to_be_visible()
                expect(page.locator('.records-day-panel')).to_contain_text('october-second.pdf')
                self.assertNotIn('october-third.pdf', page.locator('.records-day-panel').inner_text())
                expect(page.locator('.records-undated-panel')).to_contain_text('uncertain.pdf')
                self.assertIn(f'/records/{first.pk}/', page.locator('.records-day-panel .record-card__title-link').get_attribute('href'))
                self.assertIn(f'/records/{first.pk}/viewer/', page.locator('.records-day-panel').get_by_role('link', name='打开原件').get_attribute('href'))
                if artifacts:
                    folder = Path(artifacts)
                    folder.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(folder / 'records-calendar-desktop.png'), full_page=True)
                page.locator('.records-day-panel .record-card__title-link').click()
                self.assertIn(f'/records/{first.pk}/', page.url)
                page.go_back(wait_until='networkidle')
                page.locator('.records-day-panel').get_by_role('link', name='打开原件').click()
                self.assertIn(f'/records/{first.pk}/viewer/', page.url)
                page.go_back(wait_until='networkidle')
                day = page.get_by_role('link', name=re.compile('^2026-10-03，1 份资料'))
                day.focus()
                page.keyboard.press('Enter')
                expect(page.locator('.records-day-panel')).to_contain_text('october-third.pdf')
                self.assertNotIn('october-second.pdf', page.locator('.records-day-panel').inner_text())
                page.get_by_label('资料类型').select_option('LAB')
                page.get_by_role('button', name='搜索').click()
                expect(page.locator('.records-day-panel')).to_contain_text('october-third.pdf')
                page.get_by_role('link', name='列表', exact=True).click()
                page.get_by_role('link', name='日历', exact=True).click()
                expect(page.locator('.records-day-panel')).to_contain_text('october-third.pdf')
                page.get_by_role('link', name='上个月').click()
                expect(page.get_by_role('heading', name='2026年9月', exact=True)).to_be_visible()
                page.get_by_role('link', name='下个月').click()
                expect(page.get_by_role('heading', name='2026年10月', exact=True)).to_be_visible()
                page.goto(self.live_server_url + f'/records/?patient={patient.pk}&year=2100&month=12',
                          wait_until='networkidle')
                expect(page.get_by_role('link', name='下个月')).to_have_count(0)
                self.assertGreater(page.locator('.records-calendar-unavailable').count(), 0)
                self.assertEqual(page.locator('.records-calendar-unavailable a').count(), 0)
            finally:
                browser.close()

    def test_filters_and_view_toggle_work_without_phone_overflow(self):
        from playwright.sync_api import expect, sync_playwright

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'records-phone-browser')
        self.database_action(lambda: _record(
            patient, 'target-lab.pdf', document_date=date(2026, 10, 2),
            precision=DatePrecision.DAY, document_type=DocumentType.LAB,
        ))
        self.database_action(lambda: _record(
            patient, 'other-imaging.pdf', document_date=date(2026, 10, 2),
            precision=DatePrecision.DAY, document_type=DocumentType.IMAGING,
        ))
        artifacts = os.environ.get('PHR_RECORDS_CALENDAR_ARTIFACT_DIR')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                for width in (390, 320):
                    context = browser.new_context(viewport={'width': width, 'height': 800}, locale='zh-CN')
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
                                         'value': client.session.session_key, 'url': self.live_server_url}])
                    page = context.new_page()
                    page.goto(self.live_server_url + f'/records/?patient={patient.pk}&year=2026&month=10&date=2026-10-02',
                              wait_until='networkidle')
                    page.get_by_label('资料类型').select_option('LAB')
                    page.get_by_role('button', name='搜索').click()
                    expect(page.locator('.records-day-panel')).to_contain_text('target-lab.pdf')
                    self.assertNotIn('other-imaging.pdf', page.locator('.records-day-panel').inner_text())
                    page.get_by_role('link', name='列表', exact=True).click()
                    expect(page.get_by_role('link', name='列表', exact=True)).to_have_attribute('aria-current', 'page')
                    expect(page.locator('.records-groups')).to_contain_text('target-lab.pdf')
                    self.assertEqual(page.get_by_label('资料类型').input_value(), 'LAB')
                    page.get_by_role('link', name='日历', exact=True).click()
                    self.assertEqual(page.get_by_label('资料类型').input_value(), 'LAB')
                    calendar_box = page.locator('.records-calendar').bounding_box()
                    panel_box = page.locator('.records-day-panel').bounding_box()
                    self.assertGreaterEqual(panel_box['y'], calendar_box['y'] + calendar_box['height'] - 1)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    if artifacts:
                        folder = Path(artifacts)
                        folder.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(folder / f'records-calendar-{width}.png'), full_page=True)
                    context.close()
            finally:
                browser.close()
