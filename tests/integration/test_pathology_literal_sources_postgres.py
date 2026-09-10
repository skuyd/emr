"""New original-role graphs through upload and actual PostgreSQL COMMIT."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from queue import Queue

import pytest
from django.db import connection, transaction

from apps.facts.clinical_readmodels import effective_field, report_source_token
from apps.facts.clinical_services import revise_report
from apps.facts.models import Fact
from apps.facts.pathology_source import source_material
from apps.facts.revisions import FactConflict, revise_fact
from apps.processing.models import ParsingVersion
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.pipeline import DocumentProcessingPipeline
from apps.processing.runner import ExecutionState, run_processing
from apps.processing.value_objects import OcrRegion
from tests.accounts.postgres_lock_monitor import wait_until_backend_is_blocked_by
from tests.facts.pathology_factories import review
from tests.facts.test_pathology_pipeline import fixture
from tests.facts.test_pathology_split_metadata import split_rows
from tests.integration.test_family_postgres_concurrency import backend_pid, state, thread_call
from tests.processing.test_pipeline import _document_and_run, _ocr_page, _png_bytes, _Store


pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql():
    if connection.vendor != 'postgresql':
        pytest.skip('Requires an isolated PostgreSQL database')


@pytest.mark.parametrize('above_title', [False, True])
def test_real_upload_commits_new_roles_visible_to_another_connection(django_user_model, above_title):
    document, run = _document_and_run(django_user_model)
    rows = split_rows(above_title=above_title)
    page = replace(_ocr_page(), regions=tuple(OcrRegion(r.text, r.polygon, .98, i) for i, r in enumerate(rows)))
    pipeline = DocumentProcessingPipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,)))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.status == 'PUBLISHED' and version.active and version.clinical_extraction.status == 'EXTRACTED'

    def inspect_committed():
        fields = list(Fact.objects.filter(parsing_version=version, representation='FIELD'))
        for field in fields:
            source_material(field)
            for piece in field.source_fragments.all():
                piece.full_clean()
        return {f.field_key: f.automatic_content for f in fields}

    with ThreadPoolExecutor(max_workers=1) as pool:
        actual = pool.submit(thread_call, inspect_committed).result(timeout=30)
    assert actual['specimen.identity']['value']['raw'] == 'SYN-SPLIT'
    assert actual['assay.received_date']['raw_value'] == '２０３２年０６月０２日'
    assert actual['assay.report_date']['value']['value'] == '2032-06-04'
    assert actual['ihc.marker']['literal_source']['value_fragment_ordinals'] != [0]


@pytest.mark.parametrize('change', ['unselected_method', 'parent_exclude'])
def test_waiting_confirmation_rejects_committed_dependency_change_for_new_roles(django_user_model, change):
    _, patient, document, _, _ = fixture(django_user_model, name='literal-pg-' + change, rows=split_rows())
    fields = list(document.facts.filter(representation='FIELD').order_by('reading_order'))
    for field in fields:
        if field.field_key != 'ihc.score':
            review(patient, field)
    score = next(f for f in fields if f.field_key == 'ihc.score')
    token = effective_field(score)['current_source_token']
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            if change == 'parent_exclude':
                report = score.clinical_report
                revise_report(patient, actor=patient.account, report_id=report.pk, action='EXCLUDE',
                    expected_revision=report.revision_number, expected_source=report_source_token(report))
            else:
                method = next(f for f in fields if f.field_key == 'assay.method')
                review(patient, method, 'CORRECT', {'value': {'code': 'OTHER', 'raw': '合成更正方法'}, 'raw_value': '合成更正方法'})
            blocker = backend_pid()
            future = pool.submit(thread_call, lambda: revise_fact(patient, score.pk, actor=patient.account,
                action='CONFIRM', expected_revision=0, expected_source=token, checked_original=True), pids)
            waiter = pids.get(timeout=10)
            wait_until_backend_is_blocked_by(state, request_pid=waiter, blocker_pid=blocker)
        with pytest.raises(FactConflict):
            future.result(timeout=30)
    assert not score.revisions.filter(action='CONFIRM').exists()
