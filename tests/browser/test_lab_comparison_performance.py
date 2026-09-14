"""Repeatable, entirely synthetic 100-indicator / 50-report browser benchmark."""

from copy import copy
from datetime import date, timedelta
import json
import os
from pathlib import Path
import platform
from time import perf_counter
from uuid import uuid4

from django.contrib.auth import get_user_model
from apps.labs.dictionary import default_dictionary
from apps.labs.models import LabObservation
from apps.processing.models import SourceEvidence
from tests.browser import test_advanced_trends_browser as advanced
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


class TestLabComparisonPerformance(advanced.TestAdvancedTrendsBrowser):
    test_desktop_filters_independent_axes_and_opens_actual_source_image = None
    test_mobile_comparison_keyboard_scroll_and_explicit_patient_filter = None

    def test_full_matrix_scroll_response_and_alignment(self):
        from playwright.sync_api import sync_playwright

        client, patient = _patient(get_user_model(), 'comparison-performance')
        definitions = default_dictionary().indicators[:100]
        for day in range(50):
            _, first = _observation(patient, date(2026, 6, 1) + timedelta(days=day), str(day + 1))
            records, sources = [], []
            for index, definition in enumerate(definitions):
                row = first if index == 0 else copy(first)
                if index:
                    row.pk = uuid4()
                    source = copy(first.evidence)
                    source.pk = uuid4()
                    sources.append(source)
                    row.evidence = source
                row.standard_code, row.standard_name, row.raw_name = definition.code, definition.standard_name, definition.standard_name
                row.capability_level = definition.capability_level.value
                row.raw_unit = definition.unit_forms[0] if definition.unit_forms else ''
                row.reading_order = index + 1
                if index:
                    records.append(row)
                else:
                    row.save()
            SourceEvidence.objects.bulk_create(sources)
            LabObservation.objects.bulk_create(records)
        baseline = os.environ.get('PHR_COMPARISON_BASELINE') == '1'
        if os.environ.get('PHR_COMPARISON_PROFILE'):
            import cProfile
            import pstats
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            profile = cProfile.Profile()
            with CaptureQueriesContext(connection) as queries:
                response = profile.runcall(client.get, '/labs/compare/', {'patient': patient.pk})
            self.assertEqual(response.status_code, 200)
            print('Comparison SQL queries:', len(queries), 'HTML bytes:', len(response.content))
            from collections import Counter
            import re
            print('SQL tables:', Counter(re.search(r'FROM "([^"]+)"', item['sql']).group(1)
                                         for item in queries if re.search(r'FROM "([^"]+)"', item['sql'])))
            pstats.Stats(profile).strip_dirs().sort_stats('cumulative').print_stats(45)
        output = {'baseline': baseline, 'rows': 100, 'reports': 50, 'results': 5000,
                  'platform': platform.platform(), 'processor': platform.processor(),
                  'logical_cpus': os.cpu_count(), 'measurements': []}
        with sync_playwright() as playwright:
            browser, context = self._context(playwright, client, 1280)
            output['browser'] = browser.version
            page = context.new_page()
            page.set_default_timeout(120000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for width, height in ((1280, 800), (640, 400), (360, 800)):
                page.set_viewport_size({'width': width, 'height': height})
                # 640x400 CSS pixels at DPR 2 is the rendering surface of a
                # 1280x800 desktop viewport at 200% browser zoom.
                context.new_cdp_session(page).send('Emulation.setDeviceMetricsOverride', {
                    'width': width, 'height': height, 'deviceScaleFactor': 2 if width == 640 else 1, 'mobile': False})
                samples = []
                for attempt in range(2):
                    started = perf_counter()
                    response = page.goto(self.live_server_url + f'/labs/compare/?patient={patient.pk}&start=2026-06-01&end=2026-07-20', wait_until='load')
                    self.assertEqual(response.status, 200)
                    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    samples.append(round((perf_counter() - started) * 1000, 1))
                self._assert_page_width(page)
                if not baseline:
                    for button in page.locator('[data-group-toggle][aria-expanded=false]').all():
                        button.click()
                    self.assertEqual(page.locator('.comparison-indicator').count(), 100)
                    self.assertEqual(page.locator('.comparison-value').count(), 5000)
                    self.assertEqual(page.locator('.comparison-head-table th:not([hidden])').count(), 51)
                    scroll = page.locator('#comparison-results')
                    self.assertEqual(scroll.evaluate('(e) => e.scrollHeight'), scroll.evaluate('(e) => e.clientHeight'))
                    for index in (0, 49, 99):
                        row = page.locator('.comparison-indicator').nth(index)
                        row.evaluate('(e) => window.scrollTo(0, window.scrollY + e.getBoundingClientRect().top - 230)')
                        scroll.evaluate('(e) => { e.scrollLeft = 800; }')
                        page.wait_for_timeout(100)
                        head = page.locator('.comparison-head-table th').nth(7).bounding_box()
                        body = row.locator('td').nth(6).bounding_box()
                        self.assertAlmostEqual(head['x'], body['x'], delta=1)
                        self.assertGreaterEqual(head['y'], 0)
                        self.assertLess(head['y'] + head['height'], height)
                        first_column = row.locator('th').bounding_box()
                        self.assertGreaterEqual(first_column['x'], 0)
                    started = perf_counter()
                    page.locator('[data-group-toggle]').last.click()
                    page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
                    collapse_ms = round((perf_counter() - started) * 1000, 1)
                    page.locator('[data-group-toggle]').last.click()
                    self._capture(page, f'comparison-matrix-{width}.png')
                else:
                    collapse_ms = None
                output['measurements'].append({'viewport': [width, height], 'device_scale_factor': 2 if width == 640 else 1,
                                               'navigation_to_operable_ms': samples, 'collapse_ms': collapse_ms})
            self.assertEqual(errors, [])
            browser.close()
        path = os.environ.get('PHR_COMPARISON_PERFORMANCE_OUTPUT')
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(output, ensure_ascii=False))
