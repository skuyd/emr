"""Real browser review keeps candidate and typed specimen decisions separate."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.facts.models import Fact
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_cancer_narrative_browser import _Events
from tests.browser import test_pathology_browser as pathology_browser
from tests.browser.test_phase_three_browser import _db
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture
from tests.facts.test_pathology_extraction import report_rows


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestCancerTypedPathologyBrowser(SQLiteSerializedStaticLiveServerTestCase):
    def flow(self, width):
        from playwright.sync_api import expect, sync_playwright
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient, document, _, field, anchor = typed_fixture(get_user_model(), confirm_anchor=False)
        rows = report_rows()
        rows[4].text = '组织学诊断：肺癌'
        store = pathology_browser.TestPathologyBrowser.original_store(self, document, rows=rows)
        collect_current(patient, actor=patient.account)
        candidate = CancerCandidate.objects.get(source_fact=field)
        index = reverse('cancer_ordering:index') + '?patient=' + str(patient.pk)
        detail = reverse('cancer_ordering:detail', args=[candidate.pk]) + '?patient=' + str(patient.pk)
        artifact = os.environ.get('PHR_CANCER_TYPED_BROWSER_ARTIFACT_DIR')
        directory = Path(artifact) / str(width) if artifact else None
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
        with patch('apps.documents.views.originals.get_object_store', return_value=store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            try:
                context = browser.new_context(viewport={'width': width, 'height': 844}, locale='zh-CN')
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key, 'url': self.live_server_url}])
                events = _Events(self, context, self.live_server_url, 'typed-owner')
                page = context.new_page()
                page.goto(self.live_server_url + detail, wait_until='networkidle')
                expect(page.get_by_text('本条表述的核对不会确认标本信息或组织学字段。', exact=True)).to_be_visible()
                self.assertEqual(_db(lambda: resolve_ordering(patient)['profile']), 'GENERAL')
                page.get_by_role('link', name='查看组织学字段与核对历史', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.frame_locator('iframe').locator('[data-viewer-image]')).to_have_js_property('complete', True)
                self.assertGreater(page.frame_locator('iframe').locator('[data-viewer-image]').evaluate('image => image.naturalWidth'), 0)
                page.goto(self.live_server_url + f'/facts/{anchor.pk}/?patient={patient.pk}', wait_until='networkidle')
                page.get_by_label('我已对照原件核对字段、标本、检测和原文限定:', exact=True).check()
                page.get_by_role('button', name='确认原文字段', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.get_by_role('heading', name='已核对', exact=True)).to_be_visible()
                page.goto(self.live_server_url + index, wait_until='networkidle')
                page.get_by_role('button', name='重新收集当前报告表述', exact=True).click()
                page.wait_for_load_state('networkidle')
                page.goto(self.live_server_url + detail, wait_until='networkidle')
                page.get_by_label('我已打开原件并核对这条表述及所属对象', exact=False).check()
                page.get_by_role('button', name='保存本次操作', exact=True).click()
                page.wait_for_load_state('networkidle')
                expect(page.get_by_text('已核对原件 · 明确肯定 · 患者当前原发病', exact=True)).to_be_visible()
                self.assertEqual(_db(lambda: resolve_ordering(patient)['profile']), 'LUNG')
                self.assertEqual(_db(lambda: Fact.objects.get(pk=field.pk).revision_number), 0)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                if directory:
                    page.screenshot(path=str(directory / 'typed-candidate-reviewed.png'), full_page=True)
                page.goto(self.live_server_url + f'/facts/{anchor.pk}/?patient={patient.pk}', wait_until='networkidle')
                page.get_by_role('button', name='暂缓', exact=True).click()
                page.wait_for_load_state('networkidle')
                page.goto(self.live_server_url + detail, wait_until='networkidle')
                expect(page.get_by_text('来源目前不可用，请先核对对应的组织学字段及标本关联，再重新收集。', exact=True)).to_be_visible()
                self.assertEqual(_db(lambda: resolve_ordering(patient)['profile']), 'GENERAL')
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                if directory:
                    page.screenshot(path=str(directory / 'typed-anchor-deferred.png'), full_page=True)
                    (directory / 'events.json').write_text(json.dumps({'responses': events.responses, 'failures': events.failures,
                        'console': events.console, 'page_errors': events.page_errors, 'external': events.external}, indent=2), encoding='utf-8')
                events.assert_clean()
            finally:
                browser.close()

    def test_phone_typed_source_and_separate_review(self):
        self.flow(360)

    def test_desktop_typed_source_and_separate_review(self):
        self.flow(1280)
