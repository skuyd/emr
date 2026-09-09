"""Real author viewports, uploaded synthetic narratives and output lifecycles."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from PIL import Image, ImageDraw, ImageFont

from apps.cancer_ordering.models import (
    CancerCandidate, CandidateRevision, CollectionRun, DisplaySelection,
    NarrativeDependency, NarrativeSource, SelectionRevision,
)
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.documents.models import Document, DocumentPage, ProcessingRun
from apps.exports.formats import read_structured_data
from apps.exports.models import ExportJob
from apps.exports.services import generate_export
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientShare
from apps.processing.models import OcrBlock, ParsingVersion
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrPage, OcrRegion
from tests.browser.sqlite_server import SQLiteSerializedStaticLiveServerTestCase
from tests.browser.test_ac02_upload_browser import _browser_executable
from tests.browser.test_phase_three_browser import _db
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.processing.test_pipeline import _pipeline


CONTEXTS = {
    'no_fact': '主诉：肺癌治疗后不适。无发热。',
    'treatment_parent': '现病史：患者诊断为肺癌，已行化疗。',
}
EXPECTED_SOURCE = {'state': 'OMITTED', 'reason': 'SOURCE_CONTENT_NOT_SELECTED'}


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _synthetic_page(source_kind):
    """One visible image and fixed OCR with the same Unicode and pixel boxes."""
    width, height = 1000, 600
    canvas = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(Path(settings.BASE_DIR) / 'static/fonts/noto-sans-sc.ttf'), 24)
    regions = []
    for index, text in enumerate(('合成出院小结', CONTEXTS[source_kind])):
        left, top = 40, 40 + index * 85
        draw.text((left, top), text, font=font, fill='#182827', anchor='lt')
        x0, y0, x1, y1 = draw.textbbox((left, top), text, font=font, anchor='lt')
        assert 0 <= x0 < x1 < width and 0 <= y0 < y1 < height
        polygon = ((x0 / width, y0 / height), (x1 / width, y0 / height),
                   (x1 / width, y1 / height), (x0 / width, y1 / height))
        regions.append(OcrRegion(text, polygon, .98, reading_order=index))
    stream = io.BytesIO()
    canvas.save(stream, format='PNG')
    page = OcrPage(1, width, height, tuple(regions), 'fixture', 'c3b-1')
    return stream.getvalue(), page


class _Events:
    """Exact request/phase allowlist; never persist cookies or fragment tokens."""
    def __init__(self, testcase, context, base, role):
        self.testcase, self.base, self.role = testcase, base, role
        self.phase = 'setup'
        self.expected = set()
        self.requests = {}
        self.responses, self.failures, self.console, self.page_errors, self.external = [], [], [], [], []
        self.downloads = {}
        self.actual_downloads = []
        context.route('**/*', self._route)
        context.on('request', self._request)
        context.on('response', self._response)
        context.on('requestfailed', self._failed)
        context.on('page', self._page)

    def _safe_url(self, url):
        split = urlsplit(url)
        return {'origin': f'{split.scheme}://{split.netloc}', 'path': split.path,
                'query_keys': sorted(parse_qs(split.query)), 'has_fragment': bool(split.fragment)}

    def _route(self, route):
        url = route.request.url
        if url.startswith(self.base + '/'):
            route.continue_()
        else:
            self.external.append({'phase': self.phase, **self._safe_url(url)})
            route.abort()

    def _request(self, request):
        self.requests[request] = {'phase': self.phase, 'method': request.method,
                                  **self._safe_url(request.url)}

    def _response(self, response):
        record = {**self.requests[response.request], 'status': response.status}
        self.responses.append(record)

    def _failed(self, request):
        self.failures.append({**self.requests[request], 'failure': request.failure})

    def _page(self, page):
        page.on('download', lambda download: self.actual_downloads.append(
            {'phase': self.phase, **self._safe_url(download.url)}))
        page.on('pageerror', lambda error: self.page_errors.append({'phase': self.phase,
            'exception_class': type(error).__name__, 'message_sha256': _sha(str(error).encode())}))
        def console(message):
            if message.type == 'error':
                # Only this fixed browser resource-error text is needed to link
                # it to a real rejected response. Other messages are hashed.
                match = re.fullmatch(r'Failed to load resource: the server responded with a status of (\d+) \([^\n]*\)', message.text)
                self.console.append({'phase': self.phase,
                    'status': int(match[1]) if match else None,
                    'message_sha256': _sha(message.text.encode()),
                    **self._safe_url(message.location.get('url', ''))})
        page.on('console', console)

    def reject(self, method, path, status):
        self.expected.add((self.phase, method, path, status))

    def downloaded(self, path, digest):
        self.downloads[(self.phase, path)] = digest

    def assert_clean(self):
        t = self.testcase
        t.assertEqual(self.external, [], 'Unexpected non-local request')
        t.assertEqual(self.page_errors, [], 'Unexpected JavaScript exception')
        errors = [row for row in self.responses if row['status'] >= 400]
        for row in errors:
            t.assertIn((row['phase'], row['method'], row['path'], row['status']), self.expected, row)
        for row in self.console:
            t.assertTrue(row['status'] and any(
                event['status'] == row['status'] and event['path'] == row['path']
                and event['origin'] == row['origin']
                and (event['phase'], event['method'], event['path'], event['status']) in self.expected
                for event in errors), row)
        for row in self.failures:
            t.assertEqual(row['failure'], 'net::ERR_ABORTED', row)
            t.assertEqual(row['method'], 'GET', row)
            t.assertIn((row['phase'], row['path']), self.downloads, row)
        t.assertEqual(len(self.actual_downloads), len(self.downloads))
        for row in self.actual_downloads:
            t.assertIn((row['phase'], row['path']), self.downloads, row)

    def evidence(self):
        return {'role': self.role, 'expected': sorted(self.expected), 'responses': self.responses,
                'requestfailed': self.failures, 'console_errors': self.console,
                'page_errors': self.page_errors, 'non_local_requests': self.external,
                'actual_download_events': self.actual_downloads,
                'verified_downloads': [{'phase': phase, 'path': path, 'sha256': value}
                    for (phase, path), value in self.downloads.items()]}


@override_settings(DEBUG=True, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
                   PROCESSING_DISPATCH_ON_UPLOAD=False)
class TestCancerNarrativeBrowser(SQLiteSerializedStaticLiveServerTestCase):
    def _record(self, **values):
        self.evidence.update(values)

    def _capture(self, page, phase, role='author'):
        actual = page.evaluate('({width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})')
        self.assertLessEqual(actual['scroll'], actual['width'])
        name = f'{role}-{phase}.png'
        page.screenshot(path=str(self.folder / name), full_page=True,
                        mask=[page.locator('#share-link')] if page.locator('#share-link').count() else [])
        self.evidence['screenshots'].append({'path': name, 'viewport': page.viewport_size,
            'document': actual, 'sha256': _sha((self.folder / name).read_bytes()),
            'share_link_masked': bool(page.locator('#share-link').count())})

    def _row(self):
        return _db(lambda: next(row for row in candidate_rows(self.patient) if row['id'] == str(self.candidate_id)))

    def _context(self, browser, client, width, role):
        context = browser.new_context(viewport={'width': width, 'height': 844}, locale='zh-CN',
                                      timezone_id='Asia/Shanghai', accept_downloads=True)
        context.add_cookies([{'name': settings.SESSION_COOKIE_NAME,
            'value': client.session.session_key, 'url': self.live_server_url}])
        events = _Events(self, context, self.live_server_url, role)
        self.events.append(events)
        return context, context.new_page(), events

    def _confirm(self, page, events, stage):
        from playwright.sync_api import expect
        events.phase = stage
        page.get_by_label('本次操作:', exact=True).select_option('CONFIRM')
        page.get_by_label('我已打开原件并核对这条表述及所属对象:', exact=True).check()
        with page.expect_response(lambda response: response.request.method == 'POST'
                and urlsplit(response.url).path == self.detail_path) as posted:
            page.get_by_role('button', name='保存本次操作', exact=True).click()
        self.assertEqual(posted.value.status, 303)
        expect(page.locator('#candidate-current')).to_contain_text('已核对原件')
        row = self._row()
        self.assertEqual(row['status'], 'CONFIRMED')
        self.assertFalse(row['source_changed'])
        event = _db(lambda: CandidateRevision.objects.filter(candidate_id=self.candidate_id).latest('sequence'))
        self.assertEqual(event.author_id, self.patient.account_id)
        self.assertEqual(event.action, 'CONFIRM')
        self._capture(page, stage)

    def _open_original(self, page, events, stage):
        from playwright.sync_api import expect
        # A fast scripted click must not abort the detail page's still-loading
        # deferred scripts; retain the strict static-resource error checks.
        page.wait_for_load_state('load')
        events.phase = stage
        with page.expect_response(lambda response: response.request.resource_type == 'image'
                and str(self.document_id) in urlsplit(response.url).path) as image_response:
            page.get_by_role('link', name='打开原件并核对上下文', exact=True).click()
        self.assertEqual(image_response.value.status, 200)
        image = page.locator('[data-viewer-image]')
        expect(image).to_have_js_property('complete', True)
        page.wait_for_function('() => {const img=document.querySelector("[data-viewer-image]"); return img && img.naturalWidth > 0 && img.naturalHeight > 0;}')
        self.assertEqual(image.evaluate('(img) => [img.naturalWidth, img.naturalHeight]'), [1000, 600])
        self.evidence['authorized_images'].append({'stage': stage, 'status': 200, 'size': [1000, 600],
            'response_sha256': _sha(image_response.value.body())})
        self._capture(page, stage)
        # An explicit GET also avoids replaying a previous invalid review POST
        # when returning from the original image via browser history.
        page.goto(self.live_server_url + self.detail_path, wait_until='networkidle')

    def _review_and_order(self, page, events):
        from playwright.sync_api import expect
        events.phase = 'private-detail'
        page.goto(self.live_server_url + self.detail_path, wait_until='networkidle')
        expect(page.locator('#candidate-original')).to_contain_text('原始采集快照')
        expect(page.locator('#candidate-source')).to_contain_text(CONTEXTS[self.source_kind])
        expect(page.locator('#candidate-source')).to_contain_text('来源栏目：' + ('主诉' if self.source_kind == 'no_fact' else '现病史'))
        expect(page.locator('#candidate-current')).to_contain_text('肺癌')
        self.assertEqual(page.locator('a[href^="/facts/"]').count(), 0)
        original = self._row()['original_data']
        events.phase = 'invalid-review'
        events.reject('POST', self.detail_path, 400)
        with page.expect_response(lambda response: response.request.method == 'POST'
                and urlsplit(response.url).path == self.detail_path) as posted:
            page.get_by_role('button', name='保存本次操作', exact=True).click()
        self.assertEqual(posted.value.status, 400)
        expect(page.get_by_role('alert')).to_contain_text('本次尚未保存')
        checkbox = page.get_by_label('我已打开原件并核对这条表述及所属对象:', exact=True)
        self.assertIn('id_checked_original_error', checkbox.get_attribute('aria-describedby') or '')
        self.assertEqual(_db(lambda: (CandidateRevision.objects.count(), SelectionRevision.objects.count())), (0, 0))
        self._capture(page, 'invalid-review')
        self._open_original(page, events, 'authorized-original')
        self._confirm(page, events, 'confirmed')
        prior_profile = _db(lambda: resolve_ordering(self.patient)['profile'])
        for action, status in [('DEFER', 'DEFERRED'), ('UNDO', 'CONFIRMED')]:
            events.phase = action.lower()
            page.get_by_label('本次操作:', exact=True).select_option(action)
            with page.expect_response(lambda response: response.request.method == 'POST'
                    and urlsplit(response.url).path == self.detail_path) as posted:
                page.get_by_role('button', name='保存本次操作', exact=True).click()
            self.assertEqual(posted.value.status, 303)
            expect(page.locator('#candidate-current')).to_contain_text('暂不处理' if action == 'DEFER' else '已核对原件')
            self.assertEqual(self._row()['status'], status)
            self.assertEqual(_db(lambda: resolve_ordering(self.patient)['profile']), 'GENERAL' if action == 'DEFER' else prior_profile)
        self.assertEqual(self._row()['original_data'], original)
        self.assertEqual(_db(lambda: list(CandidateRevision.objects.order_by('sequence').values_list('action', flat=True))), ['CONFIRM', 'DEFER', 'UNDO'])
        events.phase = 'ordering'
        page.get_by_role('link', name='癌种与指标顺序', exact=True).click()
        for phase in ['manual', 'restore', 'manual-final']:
            events.phase = phase
            if phase == 'restore':
                page.get_by_role('button', name='撤销上次选择', exact=True).click()
                expect(page.get_by_role('heading', name='当前显示顺序：' + ('肺癌指标顺序' if prior_profile == 'LUNG' else '通用顺序'), exact=True)).to_be_visible()
                self.assertEqual(_db(lambda: resolve_ordering(self.patient)['selection_state']['mode']), 'AUTO')
            else:
                page.get_by_label('排列方式:', exact=True).select_option('MANUAL_PROFILE')
                page.get_by_label('手动显示顺序:', exact=True).select_option('PANCREAS')
                page.get_by_role('button', name='保存显示顺序', exact=True).click()
                expect(page.get_by_role('heading', name='当前显示顺序：胰腺癌指标顺序', exact=True)).to_be_visible()
            self._capture(page, phase)

    def _select_output(self, page):
        self.assertEqual(page.locator('input[name="cancer_candidate_ids"]:checked').count(), 0)
        self.assertFalse(page.locator('input[name="include_indicator_ordering"]').is_checked())
        for name in ('document_ids', 'fact_ids', 'report_ids', 'clinical_field_ids'):
            self.assertEqual(page.locator(f'input[name="{name}"]:checked').count(), 0)
        if page.locator('select[name="mode"]').count():
            page.locator('select[name="mode"]').select_option('documents')
        for option in page.locator('input[name="sections"]').all():
            option.uncheck()
        page.locator('input[name="sections"][value="cancer_ordering"]').check()
        if page.locator('input[name="details"]').count():
            page.locator('input[name="details"]').check()
        page.locator(f'input[name="cancer_candidate_ids"][value="{self.candidate_id}"]').check()
        page.locator('input[name="include_indicator_ordering"]').check()

    def _private_absent(self, text):
        for secret in (CONTEXTS[self.source_kind], self.parent_id,
                       str(self.patient.account_id), self._row()['current_source_token']):
            if secret:
                self.assertNotIn(secret, text)
        self.assertNotIn('cancer_ordering_fingerprint', text)
        self.assertNotIn('cancer_ordering_dependency', text)

    def _output(self, author, author_events, reader, reader_events, store, round_name):
        from playwright.sync_api import expect
        author_events.phase = round_name + '-prepare'
        author.goto(self.live_server_url + f'/visit/?patient={self.patient.pk}', wait_until='networkidle')
        self._select_output(author)
        author.get_by_role('button', name='预览内容与导出清单', exact=True).click()
        expect(author.get_by_role('heading', name='确认本次内容', exact=True)).to_be_visible()
        preview_url = author.url
        job_id = urlsplit(preview_url).path.split('/')[2]
        job = _db(lambda: ExportJob.objects.get(pk=job_id))
        self.assertEqual(job.status, 'PREVIEW')
        self.assertEqual(job.snapshot['documents'], [])
        self.assertEqual(job.snapshot['cancer_candidates'][0]['source'], EXPECTED_SOURCE)
        self.assertTrue(_db(lambda: job.source_bindings.filter(document_id=self.document_id).exists()))
        for text in ['肺癌', '已核对原件', '胰腺癌指标顺序']:
            expect(author.locator('main')).to_contain_text(text)
        self._private_absent(author.locator('main').inner_text())
        self._capture(author, round_name + '-preview')
        author_events.phase = round_name + '-generate'
        author.get_by_label('导出格式:', exact=True).select_option('zip')
        author.get_by_role('button', name='确认清单并生成', exact=True).click()
        expect(author.get_by_text('正在准备文件。', exact=False)).to_be_visible()
        self.assertEqual(_db(lambda: ExportJob.objects.get(pk=job_id).status), 'QUEUED')
        _db(lambda: generate_export(job_id, store))
        job = _db(lambda: ExportJob.objects.get(pk=job_id))
        self.assertEqual(job.status, 'READY')
        self.assertGreater(job.byte_size, 0)
        self.assertIn(job.object_key, store.objects)
        author.reload(wait_until='networkidle')
        author_events.phase = round_name + '-download'
        with author.expect_download() as pending:
            author.get_by_role('link', name='下载 records.zip', exact=True).click()
        download = pending.value
        self.assertIsNone(download.failure())
        output_path = self.folder / (round_name + '-records.zip')
        download.save_as(str(output_path))
        payload = output_path.read_bytes()
        self.assertEqual(_sha(payload), job.sha256)
        self.assertEqual(len(payload), job.byte_size)
        self.assertEqual(payload, store.objects[job.object_key])
        author_events.downloaded(f'/visit/{job_id}/download/', job.sha256)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read('manifest.json'))
            for item in manifest['files']:
                raw = archive.read(item['path'])
                self.assertEqual((_sha(raw), len(raw)), (item['sha256'], item['byte_size']))
            data = read_structured_data(archive.read('records.json'))
            self.assertEqual(data['documents'], [])
            self.assertEqual([item['id'] for item in data['cancer_candidates']], [str(self.candidate_id)])
            self.assertEqual(data['cancer_candidates'][0]['source'], EXPECTED_SOURCE)
            self.assertEqual(data['indicator_ordering'][0]['mode'], 'MANUAL_PROFILE')
            self.assertFalse(any(name.startswith('originals/') for name in archive.namelist()))
            self._private_absent(json.dumps(data, ensure_ascii=False))
        author_events.phase = round_name + '-share-form'
        author.goto(self.live_server_url + f'/patients/{self.patient.pk}/shares/', wait_until='networkidle')
        self._select_output(author)
        author.get_by_role('button', name='生成分享链接', exact=True).click()
        expect(author.get_by_label('分享链接', exact=True)).to_be_visible()
        link = author.get_by_label('分享链接', exact=True).input_value()
        reader_events.phase = round_name + '-exchange'
        try:
            reader.goto(link, wait_until='domcontentloaded')
        except Exception:
            raise AssertionError('Synthetic share navigation did not complete') from None
        reader.wait_for_url(re.compile(r'.*/shared/[0-9a-f-]{36}/$'), wait_until='domcontentloaded')
        expect(reader.get_by_role('heading', name='只读资料分享', exact=True)).to_be_visible()
        self.assertNotIn('#', reader.url)
        shared_path = urlsplit(reader.url).path
        shared_id = shared_path.split('/')[2]
        shared = _db(lambda: PatientShare.objects.get(pk=shared_id))
        self.assertFalse(_db(lambda: shared.source_bindings.exists()))
        self.assertEqual(reader.evaluate('sessionStorage.getItem("phr:pending-share")'), None)
        self.assertTrue(any(row['method'] == 'POST' and row['status'] == 200
                            and row['phase'] == reader_events.phase for row in reader_events.responses))
        for text in ['肺癌', '已核对原件', '胰腺癌指标顺序']:
            expect(reader.locator('[data-share-content]')).to_contain_text(text)
        self._private_absent(reader.locator('main').inner_text())
        self.assertEqual(reader.get_by_role('link', name='查看这份原件', exact=True).count(), 0)
        original_path = f'{shared_path}documents/{self.document_id}/'
        reader_events.phase = round_name + '-source-refused'
        reader_events.reject('GET', original_path, 404)
        self.assertEqual(reader.evaluate('async (url) => (await fetch(url)).status', original_path), 404)
        self._capture(reader, round_name + '-share', 'reader')
        result = {'job_id': job_id, 'preview_path': urlsplit(preview_url).path,
                  'download_path': f'/visit/{job_id}/download/', 'share_id': shared_id,
                  'shared_path': shared_path, 'zip_sha256': job.sha256, 'byte_size': job.byte_size}
        self.evidence['outputs'].append(result)
        return result

    def _change_source(self, output):
        def change():
            saved_candidate = CancerCandidate.objects.values().get(pk=self.candidate_id)
            selection_before = list(DisplaySelection.objects.values())
            original_ocr = list(OcrBlock.objects.filter(parsing_version__document_id=self.document_id).order_by('pk').values())
            original_parent = Fact.objects.get(pk=self.parent_id) if self.parent_id else None
            source_fields = ('raw_text', 'automatic_content', 'document_page_id', 'parsing_version_id')
            original_parent_source = {key: deepcopy(getattr(original_parent, key)) for key in source_fields} if original_parent else None
            job = ExportJob.objects.get(pk=output['job_id'])
            snapshot = deepcopy(job.snapshot)
            before_token = next(row for row in candidate_rows(self.patient) if row['id'] == str(self.candidate_id))['current_source_token']
            if original_parent:
                old_revision = original_parent.revision_number
                revise_fact(self.patient, original_parent.pk, actor=self.patient.account, action='CONFIRM',
                            expected_revision=old_revision, checked_original=True)
                original_parent.refresh_from_db()
                self.assertEqual(original_parent.revision_number, old_revision + 1)
                self.assertEqual({key: getattr(original_parent, key) for key in source_fields}, original_parent_source)
                mutation = {'kind': 'parent_confirm', 'previous_revision': old_revision,
                            'current_revision': original_parent.revision_number,
                            'raw_text_automatic_content_and_original_location_unchanged': True}
            else:
                new_document, _ = parsed_facts(self.patient, ['主诉：合成新增不适。'])
                self.assertFalse(Fact.objects.filter(document=new_document).exists())
                self.assertFalse(NarrativeSource.objects.filter(document=new_document).exists())
                self.assertFalse(CollectionRun.objects.filter(document=new_document).exists())
                self.assertFalse(job.source_bindings.filter(document=new_document).exists())
                mutation = {'kind': 'new_uncollected_input', 'document_id': str(new_document.pk),
                            'fact_count': 0, 'narrative_count': 0, 'collection_count': 0, 'old_binding': False}
            self.assertEqual(list(OcrBlock.objects.filter(parsing_version__document_id=self.document_id).order_by('pk').values()), original_ocr)
            self.assertEqual(CancerCandidate.objects.values().get(pk=self.candidate_id), saved_candidate)
            self.assertEqual(list(DisplaySelection.objects.values()), selection_before)
            job.refresh_from_db()
            self.assertEqual((job.status, job.snapshot), ('READY', snapshot))
            current = next(row for row in candidate_rows(self.patient) if row['id'] == str(self.candidate_id))
            if original_parent:
                self.assertNotEqual(current['current_source_token'], before_token)
                self.assertTrue(current['source_changed'])
            mutation['job_still_ready_before_consumer'] = True
            mutation['original_ocr_and_candidate_and_selection_unchanged'] = True
            return mutation
        self.evidence['mutation'] = _db(change)

    def _reject_old(self, author, author_events, reader, reader_events, output, stage, *, refresh_live):
        from playwright.sync_api import expect
        for name in ['preview', 'download']:
            author_events.phase = stage + '-' + name
            path = output[name + '_path']
            author_events.reject('GET', path, 409)
            count = len(author_events.actual_downloads)
            response = author.goto(self.live_server_url + path, wait_until='networkidle')
            self.assertEqual(response.status, 409)
            self.assertNotIn('肺癌', author.locator('main').inner_text())
            self.assertEqual(len(author_events.actual_downloads), count)
        shared_path = output['shared_path']
        reader_events.phase = stage + '-share'
        reader_events.reject('GET', shared_path + 'status/', 410)
        if refresh_live:
            with reader.expect_response(lambda response: urlsplit(response.url).path == shared_path + 'status/') as status:
                reader.evaluate("window.dispatchEvent(new Event('pageshow'))")
            self.assertEqual(status.value.status, 410)
            expect(reader.get_by_role('alert')).to_contain_text('分享已失效')
            self.assertNotIn('肺癌', reader.locator('[data-share-content]').inner_text())
            self._capture(reader, stage + '-live-expired', 'reader')
        reader_events.reject('GET', shared_path, 410)
        response = reader.goto(self.live_server_url + shared_path, wait_until='networkidle')
        self.assertEqual(response.status, 410)
        self.assertNotIn('肺癌', reader.locator('main').inner_text())
        self._capture(reader, stage + '-expired', 'reader')
        def state():
            job = ExportJob.objects.get(pk=output['job_id'])
            shared = PatientShare.objects.get(pk=output['share_id'])
            self.assertEqual((job.status, job.snapshot, job.options), ('INVALIDATED', {}, {}))
            self.assertEqual(shared.snapshot, {})
            self.assertIsNotNone(shared.invalidated_at)
            return {'job_status': job.status, 'snapshot_cleared': True, 'share_invalidated': True}
        self.evidence[stage] = _db(state)

    def _flow(self, width, source_kind):
        from playwright.sync_api import expect, sync_playwright
        cache.clear()
        executable = _browser_executable()
        self.assertIsNotNone(executable, 'A local Chromium is required; this browser contract may not skip')
        self.source_kind = source_kind
        root = os.environ.get('PHR_CANCER_BROWSER_ARTIFACT_DIR')
        temporary = tempfile.TemporaryDirectory(prefix='cancer-narrative-browser-') if not root else None
        root = Path(root or temporary.name)
        self.folder = root / f'{source_kind}-{width}'
        self.folder.mkdir(parents=True, exist_ok=False)
        self.evidence = {'source_kind': source_kind, 'author_viewport': {'width': width, 'height': 844},
                         'database_vendor': connection.vendor, 'screenshots': [], 'outputs': [],
                         'authorized_images': []}
        self.events = []
        client, self.patient = _patient(get_user_model(), f'c3b-author-{source_kind}-{width}')
        reader_client, _ = _patient(get_user_model(), f'c3b-reader-{source_kind}-{width}')
        payload, ocr = _synthetic_page(source_kind)
        (self.folder / 'synthetic-original.png').write_bytes(payload)
        self.evidence['synthetic_original'] = {'sha256': _sha(payload), 'byte_size': len(payload),
            'width': ocr.width, 'height': ocr.height, 'provider': ocr.provider,
            'provider_version': ocr.provider_version,
            'regions': [{'text': item.text, 'polygon': item.polygon, 'order': item.reading_order} for item in ocr.regions]}
        store = InMemoryObjectStore()
        try:
            with ExitStack() as stack:
                for target in ['apps.documents.views.uploads.get_object_store',
                               'apps.documents.views.originals.get_object_store', 'apps.exports.views.get_object_store']:
                    stack.enter_context(patch(target, return_value=store))
                stack.enter_context(patch('apps.exports.views.safe_enqueue_export', return_value=None))
                playwright = stack.enter_context(sync_playwright())
                browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                stack.callback(browser.close)
                self.evidence['browser_version'] = browser.version
                author_context, author, author_events = self._context(browser, client, width, 'author')
                reader_context, reader, reader_events = self._context(browser, reader_client, 360, 'reader')
                stack.callback(reader_context.close)
                stack.callback(author_context.close)
                author_events.phase = 'upload'
                author.goto(self.live_server_url + f'/uploads/new/?patient={self.patient.pk}', wait_until='networkidle')
                author.locator('#upload-file-input').set_input_files({'name': f'synthetic-{source_kind}.png',
                    'mimeType': 'image/png', 'buffer': payload})
                with author.expect_response(lambda response: response.request.method == 'POST'
                        and urlsplit(response.url).path.endswith('/content/')) as uploaded:
                    with author.expect_response(lambda response: response.request.method == 'POST'
                            and urlsplit(response.url).path == '/api/upload-batches/') as batch:
                        author.get_by_role('button', name='开始上传', exact=True).click()
                self.assertEqual(batch.value.status, 201)
                self.assertEqual(uploaded.value.status, 201)
                upload = uploaded.value.json()
                self.assertTrue(upload['saved'])
                self.document_id = upload['document_id']
                expect(author.locator('[data-file-result]')).to_contain_text('原件已保存')
                def process():
                    document = Document.objects.get(pk=self.document_id)
                    self.assertEqual((document.sha256, document.byte_size), (_sha(payload), len(payload)))
                    self.assertEqual(store.objects[document.original_object_key], payload)
                    run = ProcessingRun.objects.get(document=document)
                    self.assertEqual(run.stage, 'QUEUED')
                    result = run_processing(run.pk, _pipeline(store, ocr))
                    self.assertEqual(result.state, ExecutionState.SUCCEEDED)
                    run.refresh_from_db()
                    version = ParsingVersion.objects.get(processing_run=run)
                    self.assertTrue(version.active)
                    self.assertEqual(version.status, 'PUBLISHED')
                    self.assertEqual(run.stage, 'SUCCEEDED')
                    page = DocumentPage.objects.get(document=document)
                    self.assertEqual((page.width, page.height), (1000, 600))
                    candidate = CancerCandidate.objects.get(document=document)
                    self.assertIsNotNone(candidate.source_narrative_id)
                    self.assertIsNone(candidate.source_fact_id)
                    self.assertIsNone(candidate.source_report_id)
                    parents = list(Fact.objects.filter(document=document))
                    if source_kind == 'no_fact':
                        self.assertEqual(parents, [])
                        parent_id = None
                    else:
                        self.assertEqual(len(parents), 1)
                        self.assertEqual((parents[0].category, parents[0].representation), ('TREATMENT', 'EXCERPT'))
                        self.assertTrue(NarrativeDependency.objects.filter(narrative_source=candidate.source_narrative,
                            fact=parents[0]).exists())
                        parent_id = str(parents[0].pk)
                    return candidate.pk, parent_id, {'document_id': str(document.pk), 'processing_stage': run.stage,
                        'version_status': version.status, 'fact_count': len(parents),
                        'source_narrative': True, 'source_fact': False, 'source_report': False}
                self.candidate_id, self.parent_id, processed = _db(process)
                self._record(upload={'batch_status': 201, 'content_status': 201, 'saved': True}, processing=processed)
                expect(author.locator('[data-file-status]')).to_have_attribute('data-state', 'ORGANIZED', timeout=20000)
                self._capture(author, 'uploaded-organized')
                self.detail_path = f'/cancer-ordering/candidates/{self.candidate_id}/'
                self._review_and_order(author, author_events)
                old = self._output(author, author_events, reader, reader_events, store, 'first')
                self._change_source(old)
                self._reject_old(author, author_events, reader, reader_events, old, 'after-change', refresh_live=True)
                author_events.phase = 'recollect'
                author.goto(self.live_server_url + f'/cancer-ordering/?patient={self.patient.pk}', wait_until='networkidle')
                author.get_by_role('button', name='重新收集当前报告表述', exact=True).click()
                expect(author.get_by_role('link', name='肺癌', exact=True)).to_be_visible()
                author.wait_for_load_state('load')
                author.get_by_role('link', name='肺癌', exact=True).click()
                self._open_original(author, author_events, 'current-original-before-reconfirm')
                self._confirm(author, author_events, 'reconfirmed')
                new = self._output(author, author_events, reader, reader_events, store, 'second')
                self.assertNotEqual(old['job_id'], new['job_id'])
                self.assertNotEqual(old['share_id'], new['share_id'])
                self._reject_old(author, author_events, reader, reader_events, old, 'old-still-refused', refresh_live=False)
            for events in self.events:
                events.assert_clean()
            self.evidence['status'] = 'PASS'
        finally:
            self.evidence.setdefault('status', 'FAILED')
            self.evidence['network'] = [events.evidence() for events in self.events]
            with (self.folder / 'flow.json').open('x', encoding='utf-8', newline='\n') as output:
                json.dump(self.evidence, output, ensure_ascii=False, sort_keys=True, indent=2)
                output.write('\n')
            if temporary:
                temporary.cleanup()

    def test_phone_no_fact_lifecycle(self):
        self._flow(360, 'no_fact')

    def test_desktop_no_fact_lifecycle(self):
        self._flow(1280, 'no_fact')

    def test_phone_treatment_parent_lifecycle(self):
        self._flow(360, 'treatment_parent')

    def test_desktop_treatment_parent_lifecycle(self):
        self._flow(1280, 'treatment_parent')
