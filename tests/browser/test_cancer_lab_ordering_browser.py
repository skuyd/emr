from collections import Counter
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import override_settings

from apps.cancer_ordering.readmodels import resolve_ordering
from apps.labs.dictionary import phase_two_dictionary
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser import test_cancer_ordering_browser as _harness
from tests.browser.test_phase_three_browser import _db
from tests.cancer_ordering.test_lab_ordering import labs
from tests.cancer_ordering.test_services import _collect, _select
from tests.documents.test_detail_viewer import _patient


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestCancerLabOrderingBrowser(SQLiteSerializedStaticLiveServerTestCase):
    browser = _harness.TestCancerOrderingBrowser.browser
    capture = _harness.TestCancerOrderingBrowser.capture

    def flow(self, width):
        from playwright.sync_api import expect

        client, patient = _patient(get_user_model(), 'cancer-browser-labs-' + str(width))
        labs(patient)
        _collect(patient)
        _select(patient, 'GENERAL')
        names = {item.code: item.standard_name for item in phase_two_dictionary().indicators}
        with self.browser(client, width) as page:
            compare = self.live_server_url + f'/labs/compare/?patient={patient.pk}'
            page.goto(compare, wait_until='networkidle')
            rows = lambda: [' '.join(text.split()) for text in page.locator('.comparison-table tbody tr').all_text_contents()]
            original_rows = Counter(rows())
            self.assertGreaterEqual(sum(original_rows.values()), 6)
            for profile, label, first_code, selector in (
                ('LUNG', '肺癌指标顺序', 'LAB_CEA', ['LAB_CEA', 'LAB_WBC', 'LAB_CA19_9']),
                ('PANCREAS', '胰腺癌指标顺序', 'LAB_CA19_9', ['LAB_CA19_9', 'LAB_CEA', 'LAB_WBC']),
            ):
                page.get_by_role('link', name='调整显示顺序', exact=True).click()
                page.get_by_label('排列方式:', exact=True).select_option('MANUAL_PROFILE')
                page.get_by_label('手动显示顺序:', exact=True).select_option(profile)
                page.get_by_role('button', name='保存显示顺序', exact=True).focus()
                page.keyboard.press('Enter')
                expect(page.get_by_role('heading', name='当前显示顺序：' + label, exact=True)).to_be_visible()
                page.goto(compare, wait_until='networkidle')
                expect(page.locator('main')).to_contain_text('指标显示顺序：' + label)
                self.assertEqual(Counter(rows()), original_rows)
                expect(page.locator('.comparison-table tbody th[scope="row"]').first).to_contain_text(names[first_code])
                self.capture(page, f'caller-comparison-{profile.lower()}-{width}.png')
                page.goto(self.live_server_url + f'/trends/?patient={patient.pk}', wait_until='networkidle')
                expect(page.locator('.trend-summary-card h2').first).to_contain_text(names[first_code])
                explicit = ['LAB_WBC', 'LAB_CA19_9', 'LAB_CEA']
                query = urlencode([('patient', str(patient.pk)), *[('code', code) for code in explicit]])
                page.goto(self.live_server_url + '/trends/compare/?' + query, wait_until='networkidle')
                self.assertEqual(page.locator('input[name="code"]').evaluate_all('(nodes) => nodes.map(n => n.value)'), selector)
                identifiers = page.locator('.trend-series h2').evaluate_all('(nodes) => nodes.map(n => n.id)')
                self.assertEqual([identifier.rsplit('-', 1)[0].removeprefix('series-') for identifier in identifiers], explicit)
                self.capture(page, f'caller-joint-{profile.lower()}-{width}.png')
            self.assertEqual(_db(lambda: resolve_ordering(patient)['profile']), 'PANCREAS')

    def test_phone_actual_preference_preserves_cells_and_explicit_graph_order(self):
        self.flow(360)

    def test_desktop_actual_preference_preserves_cells_and_explicit_graph_order(self):
        self.flow(1280)
