from copy import copy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from playwright.sync_api import expect, sync_playwright

from apps.labs.report_workspace import report_workspace
from apps.labs.revisions import effective_observation
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient, _pdf_bytes
from tests.labs.test_report_relations import report


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class TestLabReportWorkspaceBrowser(StaticLiveServerTestCase):
    def test_desktop_layout_with_larger_browser_font_keeps_original_beside_editor(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'report-workspace-large-browser-font')
        document, first, _unit = report(patient)
        store = InMemoryObjectStore()
        store.objects[document.original_object_key] = _pdf_bytes()
        selected = next(item for item in report_workspace(patient)['reports'] if first.pk in {row.pk for row in item['rows']})
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                context = browser.new_context(viewport={'width': 1055, 'height': 850})
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                     'url': self.live_server_url}])
                page = context.new_page()
                context.new_cdp_session(page).send('Page.setFontSizes', {'fontSizes': {'standard': 20}})
                response = page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}&report={selected["key"]}',
                                     wait_until='networkidle')
                assert response.status == 200
                assert page.evaluate("matchMedia('(max-width:58rem)').matches")
                assert not page.evaluate("matchMedia('(max-width:928px)').matches")
                original = page.locator('.labs-report-original').bounding_box()
                editor = page.locator('.labs-report-editor').bounding_box()
                assert original['x'] + original['width'] <= editor['x']
                page.set_viewport_size({'width': 1440, 'height': 720})
                page.locator('[data-report-source-page]').evaluate('''tab => {
                    for (let index = 2; index <= 25; index++) {
                        const copy = tab.cloneNode(true);
                        copy.textContent = `来源 ${index} · 第 1 页`;
                        tab.parentElement.append(copy);
                    }
                }''')
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                caption = page.locator('.labs-report-source-caption').bounding_box()
                dock = page.locator('.labs-report-actions').bounding_box()
                assert caption['y'] + caption['height'] <= dock['y']
            finally:
                browser.close()

    def test_desktop_and_phone_edit_confirm_and_navigate(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        store = InMemoryObjectStore()
        cases = []
        for width in (1280, 360):
            client, patient = _patient(get_user_model(), f'report-workspace-browser-{width}')
            document, first, _unit = report(patient)
            second = copy(first)
            second.pk, second.reading_order, second.raw_name = uuid.uuid4(), first.reading_order + 1, '血红蛋白'
            second.save(force_insert=True)
            following, _, _ = report(patient, number=f'NEXT{width}', value='8')
            store.objects[document.original_object_key] = _pdf_bytes()
            store.objects[following.original_object_key] = _pdf_bytes()
            selected = next(item for item in report_workspace(patient)['reports'] if first.pk in {row.pk for row in item['rows']})
            cases.append((width, client, patient, first, second, selected))
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for width, client, patient, first, second, selected in cases:
                    context = browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width == 360)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                         'url': self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    response = page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}&report={selected["key"]}',
                                         wait_until='networkidle')
                    assert response.status == 200
                    expect(page.locator('[data-report-observation]')).to_have_count(2)
                    assert page.locator('[data-report-image]').evaluate('image => image.complete && image.naturalWidth > 0')
                    if width == 1280:
                        page.locator('.labs-report-identity > summary').click()
                        sampled_at = page.get_by_label('采样时间').first
                        original_time = sampled_at.input_value()
                        sampled_at.fill('2026-09-17')
                        page.get_by_role('button', name='保存修改').click()
                        expect(sampled_at).to_be_focused()
                        assert sampled_at.evaluate('(input) => !input.validity.valid')
                        sampled_at.fill(original_time)
                    if width == 360:
                        page.get_by_role('button', name='收起原图').click()
                        expect(page.locator('[data-report-image-content]')).to_be_hidden()
                        page.get_by_role('button', name='展开原图').click()
                    first_row = page.locator(f'[data-report-observation="{first.pk}"]')
                    second_row = page.locator(f'[data-report-observation="{second.pk}"]')
                    first_row.get_by_label('结果原文').fill('7')
                    second_row.get_by_label('结果原文').fill('9')
                    page.get_by_role('link', name='下一份').click()
                    expect(page.get_by_role('dialog', name='有未保存的修改')).to_be_visible()
                    page.get_by_role('button', name='继续编辑').click()
                    expect(first_row.get_by_label('结果原文')).to_have_value('7')
                    expect(second_row.get_by_label('结果原文')).to_have_value('9')
                    first_row.get_by_role('button', name='第 1 页').click()
                    expect(page.locator('[data-report-image-highlight]')).to_be_visible()
                    page.get_by_role('button', name='确认并下一份').click()
                    page.wait_for_url('**/labs/reports/review/?*', wait_until='networkidle')
                    expect(page.locator('[data-report-observation]')).to_have_count(1)
                    assert 'NEXT' in page.locator('header.labs-panel').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert errors == []
                    context.close()
            finally:
                browser.close()

    def test_compact_review_keeps_source_and_actions_accessible_and_updates_draft_counts(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'report-workspace-prototype')
        document, first, _unit = report(patient)
        extra_results = (
                ('红细胞计数', '4.50', '10^12/L', '3.80–5.10'),
                ('血红蛋白', '138', 'g/L', '115–150'),
                ('血小板计数', '220', '10^9/L', '125–350'),
                ('C 反应蛋白', '5.2', 'mg/L', '0–8.0'),
                ('红细胞沉降率', '12', 'mm/h', '0–20'))
        for index, (name, value, unit, reference) in enumerate(extra_results, start=1):
            row = copy(first)
            row.pk, row.reading_order = uuid.uuid4(), first.reading_order + index
            row.raw_name, row.raw_value, row.raw_unit, row.reference_range_raw = name, value, unit, reference
            row.save(force_insert=True)
        store = InMemoryObjectStore()
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen.canvas import Canvas

        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
        output = BytesIO()
        source = Canvas(output, pagesize=(600, 840))
        source.setFont('STSong-Light', 22)
        source.drawCentredString(300, 775, '合成医院 · 检验报告')
        source.setFont('STSong-Light', 12)
        source.drawString(40, 735, '仅供界面验证的合成资料')
        source.drawString(40, 700, '报告号：A100     采样时间：2026-09-17 08:30')
        for index, cells in enumerate((('检验项目', '结果', '单位', '参考范围'),
                ('白细胞', '5', '10^9/L', ''), *extra_results)):
            y = 640 - index * 44
            source.setStrokeColorRGB(.78, .82, .8)
            source.line(40, y - 15, 560, y - 15)
            for x, text in zip((40, 245, 330, 455), cells):
                source.drawString(x, y, text)
        source.drawString(40, 210, '本原件全部为合成数据，不代表真实检验结果。')
        source.save()
        store.objects[document.original_object_key] = output.getvalue()
        artifacts = Path(settings.BASE_DIR) / 'test-results' / 'lab-report-review'
        artifacts.mkdir(parents=True, exist_ok=True)
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                for width in (1440, 360):
                    context = browser.new_context(viewport={'width': width, 'height': 1000}, has_touch=width == 360)
                    context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                         'url': self.live_server_url}])
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}', wait_until='networkidle')
                    page.screenshot(path=str(artifacts / f'review-{width}.png'), full_page=True)
                    page.screenshot(path=str(artifacts / f'review-top-{width}.png'))
                    identity = page.locator('.labs-report-identity')
                    expect(identity).not_to_have_attribute('open', '')
                    expect(page.get_by_label('医院').first).to_be_hidden()
                    first_row = page.locator(f'[data-report-observation="{first.pk}"]')
                    value = first_row.get_by_label('结果原文', exact=True)
                    value.fill('7')
                    expect(page.locator('[data-report-image-highlight]')).to_be_visible()
                    expect(value).to_be_focused()
                    if width == 360:
                        field_box = value.bounding_box()
                        dock_box = page.locator('.labs-report-actions').bounding_box()
                        assert field_box['y'] >= page.locator('.app-header').bounding_box()['height']
                        assert field_box['y'] + field_box['height'] <= dock_box['y'], (field_box, dock_box)
                    page.screenshot(path=str(artifacts / f'review-editor-{width}.png'))
                    if width == 1440:
                        assert first_row.bounding_box()['height'] < 135
                        page.evaluate('window.scrollTo(0, 450)')
                        original = page.locator('.labs-report-original').bounding_box()
                        assert original['y'] >= page.locator('.app-header').bounding_box()['height']
                    else:
                        page.get_by_role('button', name='收起原图').click()
                        value.focus()
                        expect(page.locator('[data-report-image-content]')).to_be_hidden()
                        first_row.locator('[data-report-locate]').click()
                        expect(page.locator('[data-report-image-content]')).to_be_visible()
                        page.get_by_role('button', name='返回刚才的编辑位置').click()
                        expect(value).to_be_focused()
                    first_row.get_by_role('button', name='排除此项', exact=True).click()
                    expect(page.locator('[data-report-counts]')).to_contain_text('保留 5 项 · 排除 1 项')
                    reason = first_row.get_by_role('combobox', name='排除原因', exact=True)
                    expect(reason).to_be_visible()
                    first_row.locator('details > summary').first.click()
                    page.get_by_role('button', name='保存修改', exact=True).click()
                    expect(reason).to_be_focused()
                    reason.select_option('DUPLICATE')
                    first_row.get_by_role('button', name='恢复此项', exact=True).click()
                    expect(page.locator('[data-report-counts]')).to_contain_text('保留 6 项 · 排除 0 项')
                    page.get_by_role('button', name='添加遗漏指标', exact=True).click()
                    expect(page.locator('[data-report-counts]')).to_contain_text('保留 7 项')
                    added = page.locator('[data-report-addition]')
                    added.get_by_label('项目原文', exact=True).fill('合成补录项目')
                    added.get_by_label('结果原文', exact=True).fill('阴性')
                    page.get_by_role('button', name='移除此项', exact=True).click()
                    expect(page.locator('[data-report-counts]')).to_contain_text('保留 6 项')
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    actions = page.locator('.labs-report-actions').bounding_box()
                    assert actions['y'] >= 0 and actions['y'] + actions['height'] <= 1000
                    add_button = page.get_by_role('button', name='添加遗漏指标', exact=True).bounding_box()
                    assert add_button['y'] + add_button['height'] + 8 <= actions['y']
                    if width == 360:
                        assert actions['y'] + actions['height'] <= page.locator('.mobile-nav').bounding_box()['y']
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    for _ in range(4):
                        page.get_by_role('button', name='旋转', exact=True).click()
                        sheet = page.locator('[data-report-image-position]').bounding_box()
                        stage = page.locator('[data-report-image-stage]').bounding_box()
                        assert sheet['x'] >= stage['x'] - 1 and sheet['y'] >= stage['y'] - 1
                        assert sheet['x'] + sheet['width'] <= stage['x'] + stage['width'] + 1
                        assert sheet['y'] + sheet['height'] <= stage['y'] + stage['height'] + 1
                    assert errors == []
                    context.close()
            finally:
                browser.close()

    def test_exclude_restore_and_additions_save_with_existing_report_contract(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'report-workspace-row-actions')
        document, first, _unit = report(patient)
        store = InMemoryObjectStore()
        store.objects[document.original_object_key] = _pdf_bytes()
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                context = browser.new_context(viewport={'width': 1280, 'height': 850})
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                     'url': self.live_server_url}])
                page = context.new_page()
                page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}', wait_until='networkidle')
                first_row = page.locator(f'[data-report-observation="{first.pk}"]')
                expect(page.locator('[name=specimen], [name=method_raw]')).to_have_count(0)
                first_row.get_by_label('结果原文').fill('7')
                first_row.get_by_role('button', name='排除此项', exact=True).click()
                first_row.get_by_role('combobox', name='排除原因', exact=True).select_option('DUPLICATE')
                operation = page.locator('[data-report-form]').get_attribute('data-operation-id')
                page.get_by_role('button', name='保存修改', exact=True).click()
                expect(page.locator('[data-report-form]')).not_to_have_attribute('data-operation-id', operation)
                expect(first_row.get_by_label('结果原文')).to_have_value('7')
                expect(first_row.get_by_role('button', name='恢复此项', exact=True)).to_be_visible()
                expect(first_row.get_by_role('combobox', name='排除原因', exact=True)).to_have_value('DUPLICATE')
                first_row.get_by_role('button', name='恢复此项', exact=True).click()
                page.get_by_role('button', name='添加遗漏指标', exact=True).click()
                added = page.locator('[data-report-addition]')
                expect(added.locator('[name=specimen], [name=method_raw]')).to_have_count(0)
                added.get_by_label('项目原文', exact=True).fill('合成补录项目')
                added.get_by_label('结果原文', exact=True).fill('阴性')
                operation = page.locator('[data-report-form]').get_attribute('data-operation-id')
                page.get_by_role('button', name='保存修改', exact=True).click()
                expect(page.locator('[data-report-form]')).not_to_have_attribute('data-operation-id', operation)
                expect(page.locator('[data-report-observation]')).to_have_count(2)
                expect(page.locator('[data-report-counts]')).to_contain_text('保留 2 项 · 排除 0 项')
                expect(page.get_by_role('article', name='合成补录项目').get_by_label('结果原文')).to_have_value('阴性')
                expect(first_row.get_by_role('button', name='排除此项', exact=True)).to_be_visible()
            finally:
                browser.close()
        saved = effective_observation(first)
        assert (saved.raw_value, saved.specimen, saved.method_raw) == ('7', 'BLOOD', '合成方法A')

    def test_phone_returns_to_editing_a_field_inside_collapsed_details(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'report-workspace-hidden-return')
        document, _, _ = report(patient)
        store = InMemoryObjectStore()
        store.objects[document.original_object_key] = _pdf_bytes()
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                context = browser.new_context(viewport={'width': 360, 'height': 850}, has_touch=True)
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                     'url': self.live_server_url}])
                page = context.new_page()
                page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}', wait_until='networkidle')
                row = page.locator('[data-report-observation]').first
                more = row.locator('.labs-report-more > summary')
                more.click()
                phase = row.get_by_label('本次检测生理阶段', exact=True)
                phase.focus()
                phase.select_option('卵泡期')
                more.click()
                expect(phase).to_be_hidden()
                row.locator('[data-report-locate]').click()
                page.get_by_role('button', name='返回刚才的编辑位置').click()
                expect(phase).to_be_visible()
                expect(phase).to_be_focused()
                expect(phase).to_have_value('卵泡期')
            finally:
                browser.close()

    def test_manual_addition_defaults_to_the_source_being_viewed(self):
        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        client, patient = _patient(get_user_model(), 'report-workspace-add-source')
        first_doc, _, _ = report(patient)
        second_doc, _, second_unit = report(patient)
        store = InMemoryObjectStore()
        store.objects[first_doc.original_object_key] = store.objects[second_doc.original_object_key] = _pdf_bytes()
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                context = browser.new_context(viewport={'width': 1280, 'height': 850})
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.session.session_key,
                                     'url': self.live_server_url}])
                page = context.new_page()
                page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}', wait_until='networkidle')
                page.locator('[data-report-source-page]').nth(1).click()
                page.get_by_role('button', name='添加遗漏指标', exact=True).click()
                expect(page.locator('[data-report-addition] [name=unit_id]')).to_have_value(str(second_unit.pk))
            finally:
                browser.close()

    def test_readonly_member_can_switch_real_sources_and_locate_without_edit_actions(self):
        from apps.patients.models import PatientMembership

        executable = _browser_executable()
        if executable is None:
            self.skipTest('No supported local Chromium browser was found')
        _owner, patient = _patient(get_user_model(), 'report-workspace-source-owner')
        viewer, viewer_patient = _patient(get_user_model(), 'report-workspace-source-viewer')
        PatientMembership.objects.create(patient=patient, account=viewer_patient.account, role='VIEWER')
        first_doc, first_row, _ = report(patient)
        second_doc, _, _ = report(patient)
        store = InMemoryObjectStore()
        store.objects[first_doc.original_object_key] = store.objects[second_doc.original_object_key] = _pdf_bytes()
        with patch('apps.documents.views.originals.get_object_store', lambda: store), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                context = browser.new_context(viewport={'width': 1280, 'height': 850})
                context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': viewer.session.session_key,
                                     'url': self.live_server_url}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(self.live_server_url + f'/labs/reports/review/?patient={patient.pk}', wait_until='networkidle')
                tabs = page.locator('[data-report-source-page]')
                expect(tabs).to_have_count(2)
                tabs.nth(1).click()
                expect(tabs.nth(1)).to_have_attribute('aria-pressed', 'true')
                assert str(second_doc.pk) in page.locator('[data-report-image]').get_attribute('src')
                value = page.locator(f'[data-report-observation="{first_row.pk}"]').get_by_label('结果原文')
                expect(value).to_have_attribute('readonly', '')
                value.focus()
                expect(tabs.first).to_have_attribute('aria-pressed', 'true')
                assert str(first_doc.pk) in page.locator('[data-report-image]').get_attribute('src')
                expect(page.get_by_role('button', name='确认本报告', exact=True)).to_have_count(0)
                expect(page.locator('[data-report-exclude], [data-report-add]')).to_have_count(0)
                assert errors == []
            finally:
                browser.close()
