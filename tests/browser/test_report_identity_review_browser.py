from dataclasses import replace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from playwright.sync_api import expect, sync_playwright

from apps.labs.reports import _from_snapshot, correct_report, report_relations
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient, _pdf_bytes
from tests.labs.test_report_relations import report
from tests.labs.test_report_revision_versions import SOURCE, next_report_version


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestReportIdentityReviewBrowser(StaticLiveServerTestCase):
    def test_keyboard_and_touch_reconcile_inherited_report_corrections(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'report-reconcile-browser')
        store, cases = InMemoryObjectStore(), []
        for width, decision in ((1280, 'KEEP_REVISION'), (360, 'USE_AUTOMATIC')):
            document, _, original = report(patient, number=f'REPARSE{width}')
            store.objects[document.original_object_key] = _pdf_bytes()
            correct_report(patient, patient.account, original.pk, {'institution': '人工核对医院'}, expected_revision=0,
                           source_evidence=SOURCE, rationale='依据原件医院', operation_id='hospital')
            current, = next_report_version(original, (replace(_from_snapshot(original.automatic), report_number=f'NEW{width}'),))
            cases.append((width, decision, current))
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for width, decision, unit in cases:
                    context = browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 360)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                         'url': self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    response = page.goto(self.live_server_url + f'/labs/reports/{unit.pk}/', wait_until='networkidle')
                    assert response.status == 200
                    assert page.locator('[data-report-original]').evaluate('image => image.complete && image.naturalWidth > 0')
                    expect(page.get_by_text('本次识别值：', exact=False)).to_be_visible()
                    expect(page.locator('select[name="decision"]')).to_have_accessible_name('核对结论')
                    page.get_by_role('combobox', name='核对结论', exact=True).select_option(decision)
                    page.get_by_role('combobox', name='核对的原件位置', exact=True).select_option('0')
                    expect(page.locator('[data-report-highlight]')).to_be_visible()
                    page.get_by_label('新旧依据的核对说明').fill('逐项对照本次识别与原件')
                    button = page.get_by_role('button', name='保存差异核对结论')
                    if width == 360:
                        button.tap()
                    else:
                        button.focus()
                        page.keyboard.press('Enter')
                    expect(page.get_by_role('button', name='保存差异核对结论')).to_have_count(0)
                    expect(page.locator('dl dd').filter(has_text='人工核对医院' if decision == 'KEEP_REVISION' else '合成医院')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert errors == []
                    context.close()
            finally:
                browser.close()

    def test_keyboard_and_touch_review_source_correct_time_and_revoke_relation(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'report-review-browser')
        store = InMemoryObjectStore()
        cases = []
        for width in (1280, 360):
            left_doc, _, left = report(patient, number=f'R{width}')
            right_doc, _, right = report(patient, number=f'R{width}')
            store.objects[left_doc.original_object_key] = store.objects[right_doc.original_object_key] = _pdf_bytes()
            relation = next(item for item in report_relations(patient)
                            if {item.left_key, item.right_key} == {left.source_key, right.source_key})
            cases.append((width, left, relation))
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for width, left, relation in cases:
                    context = browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 360)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                         'url': self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    response = page.goto(self.live_server_url + '/labs/reports/', wait_until='networkidle')
                    assert response.status == 200
                    page.goto(self.live_server_url + f'/labs/report-relations/{relation.pk}/', wait_until='networkidle')
                    page.get_by_label('原件核对依据').fill('对照原件后撤销关联')
                    button = page.get_by_role('button', name='撤销归并')
                    if width == 360:
                        button.tap()
                    else:
                        button.focus()
                        page.keyboard.press('Enter')
                    expect(page.get_by_text('当前结论：已撤销。关联不会删除图片或结果。')).to_be_visible()
                    page.goto(self.live_server_url + f'/labs/reports/{left.pk}/', wait_until='networkidle')
                    assert page.locator('[data-report-original]').evaluate('image => image.complete && image.naturalWidth > 0')
                    page.get_by_label('原件上的正确内容').fill('2026-09-17 11:45')
                    source = page.get_by_label('原件位置')
                    source.select_option('0')
                    expect(page.locator('[data-report-highlight]')).to_be_visible()
                    page.get_by_label('核对依据', exact=True).fill('核对原件采样时间')
                    page.get_by_role('button', name='保存原件核对与更正').click()
                    expect(page.locator('dd').filter(has_text='2026-09-17 11:45')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert errors == []
                    context.close()
            finally:
                browser.close()
