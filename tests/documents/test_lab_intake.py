from dataclasses import replace
import io

from PIL import Image
from pypdf import PdfWriter
import pytest

from apps.documents.inspection import inspect_upload
from apps.documents.intake import register_intake, run_intake, cleanup_intake
from apps.documents.models import Document, UploadBatch, UploadItem, UploadIntake, ProcessingRun
from apps.labs.models import LabObservation, LabReportUnit
from apps.processing.models import OcrBlock
from apps.processing.runner import run_processing, ExecutionState
from tests.documents.fakes import InMemoryObjectStore
from tests.documents.test_detail_viewer import _patient
from tests.processing.test_pipeline import _ocr_page, _pipeline, _region


pytestmark = pytest.mark.django_db(transaction=True)


def lab_page(*, time='2026-09-17 08:30', report='A1', page_marker='', value='4.20'):
    original = _ocr_page(value)
    regions = list(original.regions)
    regions[1] = replace(regions[1], text='采样时间：' + time if time else '检验结果续页')
    regions.append(_region('报告号：' + report, .05, .8, .7, 7))
    if page_marker:
        regions.append(_region(page_marker, .05, .8, .8, 8))
    return replace(original, regions=tuple(regions))


def stage(patient, store, *, batch=None, ordinal=1, shade=0):
    batch = batch or UploadBatch.objects.create(patient=patient, file_count=1, created_by=patient.account)
    item = UploadItem.objects.create(batch=batch, ordinal=ordinal, display_filename=f'synthetic-{ordinal}.png', status='UPLOADING')
    payload = io.BytesIO()
    Image.new('RGB', (100, 100), (shade, 20, 30)).save(payload, format='PNG')
    payload.seek(0)
    with inspect_upload(payload, item.display_filename) as inspected:
        with inspected.open() as source:
            staged = store.put_staging(source, expected_size=inspected.byte_size, expected_sha256=inspected.sha256)
        outcome = register_intake(patient, batch.pk, item.pk, inspected, staged, store, actor=patient.account)
    return item, outcome


def test_missing_sampling_time_never_creates_archive_or_permanent_ocr(django_user_model):
    _, patient = _patient(django_user_model, 'intake-reject')
    store = InMemoryObjectStore()
    item, outcome = stage(patient, store)
    assert outcome.kind.value == 'VALIDATING' and outcome.document_id is None
    assert not Document.objects.exists()
    assert run_intake(item.pk, _pipeline(store, lab_page(time='2026-09-17')), store) == 'SETTLED'
    item.refresh_from_db()
    intake = UploadIntake.objects.get(item=item)
    assert item.status == 'REJECTED'
    assert item.error_code == 'sampling_time_missing'
    assert not Document.objects.exists() and not ProcessingRun.objects.exists()
    assert not OcrBlock.objects.exists() and not LabObservation.objects.exists()
    assert intake.recognition == {} and intake.staged == {}
    assert store.objects == {}


def test_admission_then_formal_parse_reuses_ocr_and_persists_report_identity(django_user_model):
    _, patient = _patient(django_user_model, 'intake-accept')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    pipeline = _pipeline(store, lab_page())
    assert run_intake(item.pk, pipeline, store) == 'SETTLED'
    item.refresh_from_db()
    assert item.document_id and item.validity['status'] == 'ACCEPTED'
    assert not LabObservation.objects.exists()
    # No second OCR invocation is needed after admission.
    pipeline.raster_provider = None
    result = run_processing(ProcessingRun.objects.get(document_id=item.document_id).pk, pipeline)
    assert result.state == ExecutionState.SUCCEEDED
    row = LabObservation.objects.get()
    assert row.report_unit_id and row.raw_value == '4.20'
    assert LabReportUnit.objects.get().automatic['precision'] == 'MINUTE'


@pytest.mark.parametrize('continuation_first', [True, False])
def test_batch_continuation_waits_for_main_report_regardless_of_arrival(django_user_model, continuation_first):
    _, patient = _patient(django_user_model, 'intake-continuation')
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, file_count=2, created_by=patient.account)
    a, _ = stage(patient, store, batch=batch)
    b, _ = stage(patient, store, batch=batch, ordinal=2, shade=10)
    continuation = _pipeline(store, lab_page(time='', page_marker='第2页 共2页'))
    main = _pipeline(store, lab_page(page_marker='第1页 共2页'))
    first, second = ((a, continuation), (b, main)) if continuation_first else ((b, main), (a, continuation))
    assert run_intake(first[0].pk, first[1], store) == 'WAITING_BATCH'
    assert not Document.objects.exists()
    assert run_intake(second[0].pk, second[1], store) == 'SETTLED'
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.validity['status'] == b.validity['status'] == 'ACCEPTED'
    assert Document.objects.count() == 2
    assert UploadIntake.objects.get(item=a).recognition['units'][0]['time_source'].startswith(str(b.pk))
    for item, pipeline in ((a, continuation), (b, main)):
        assert run_processing(ProcessingRun.objects.get(document_id=item.document_id).pk, pipeline).state == ExecutionState.SUCCEEDED
    from apps.labs.readmodels import effective_rows
    assert len(effective_rows(patient, include_uncertain=True)) == 2
    from django.utils import timezone
    Document.objects.filter(pk=b.document_id).update(deleted_at=timezone.now())
    assert effective_rows(patient, include_uncertain=True) == ()


def admitted_continuation_pair(patient, store):
    batch = UploadBatch.objects.create(patient=patient, file_count=2, created_by=patient.account)
    main, _ = stage(patient, store, batch=batch)
    continuation, _ = stage(patient, store, batch=batch, ordinal=2, shade=10)
    pipelines = (_pipeline(store, lab_page(page_marker='第1页 共2页')),
                 _pipeline(store, lab_page(time='', page_marker='第2页 共2页')))
    for item, pipeline in zip((main, continuation), pipelines):
        run_intake(item.pk, pipeline, store)
    for item, pipeline in zip((main, continuation), pipelines):
        item.refresh_from_db()
        run = ProcessingRun.objects.get(document_id=item.document_id)
        assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    return main, continuation, pipelines


@pytest.mark.parametrize('change', ['unparsed', 'unchanged', 'deleted', 'report_number', 'result'])
def test_initial_continuation_parse_rechecks_admitted_time_source(django_user_model, change):
    from django.utils import timezone
    from apps.labs.reports import correct_report

    client, patient = _patient(django_user_model, 'intake-initial-source-' + change)
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, file_count=2, created_by=patient.account)
    main, _ = stage(patient, store, batch=batch)
    continuation, _ = stage(patient, store, batch=batch, ordinal=2, shade=10)
    pipelines = (_pipeline(store, lab_page(page_marker='第1页 共2页')),
                 _pipeline(store, lab_page(time='', page_marker='第2页 共2页')))
    for item, pipeline in zip((main, continuation), pipelines):
        run_intake(item.pk, pipeline, store)
    main.refresh_from_db()
    continuation.refresh_from_db()
    original = store.objects[continuation.document.original_object_key]
    if change != 'unparsed':
        assert run_processing(ProcessingRun.objects.get(document=main.document).pk, pipelines[0]).state == ExecutionState.SUCCEEDED
    if change == 'deleted':
        Document.objects.filter(pk=main.document_id).update(deleted_at=timezone.now())
    elif change == 'report_number':
        unit = LabReportUnit.objects.get(parsing_version__document=main.document)
        correct_report(patient, patient.account, unit.pk, {'report_number': 'OTHER'}, expected_revision=0,
            source_evidence={'page_number': 1, 'polygon': [[.05, .7], [.8, .7], [.8, .78], [.05, .78]]},
            rationale='核对原件报告号', operation_id='before-initial-parse')
    elif change == 'result':
        from apps.labs.revisions import revise_observation
        row = LabObservation.objects.get(parsing_version__document=main.document)
        revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': '999'},
            expected_revision=0)
    pipelines[1].raster_provider = None
    result = run_processing(ProcessingRun.objects.get(document=continuation.document).pk, pipelines[1])
    assert store.objects[continuation.document.original_object_key] == original
    accepted = change in {'unparsed', 'unchanged'}
    assert result.state == (ExecutionState.SUCCEEDED if accepted else ExecutionState.NO_STRUCTURED_RESULT)
    assert LabObservation.objects.filter(parsing_version__document=continuation.document).count() == int(accepted)
    if not accepted:
        assert not OcrBlock.objects.filter(parsing_version__document=continuation.document).exists()
        from apps.documents.batches import document_validity_label
        label = document_validity_label(continuation.document)
        assert '采样时间来源不可用或关联已撤销' in label
        assert '原件已保留' in label
        assert '未进入资料' not in label
        response = client.get(f'/records/{continuation.document_id}/')
        assert response.status_code == 200 and label in response.content.decode()
        from apps.processing.models import ParsingVersion
        validity = ParsingVersion.objects.get(document=continuation.document, active=True).diagnostics['report_validity']
        assert validity['units'] == [{'page_number': 1, 'ordinal': 1, 'status': 'REJECTED',
                                     'reason': 'sampling_source_unavailable'}]


def test_continuation_worker_waits_for_main_original_promotion(django_user_model):
    from django.utils import timezone

    _, patient = _patient(django_user_model, 'intake-early-worker')
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, file_count=2, created_by=patient.account)
    continuation, _ = stage(patient, store, batch=batch)
    main, _ = stage(patient, store, batch=batch, ordinal=2, shade=10)
    pipeline = _pipeline(store, lab_page(time='', page_marker='第2页 共2页'))
    assert run_intake(continuation.pk, pipeline, store) == 'WAITING_BATCH'
    results = []

    def dispatch(run_id):
        run = ProcessingRun.objects.get(pk=run_id)
        if run.document_id == continuation.pk:
            results.append(run_processing(run_id, pipeline))

    assert run_intake(main.pk, _pipeline(store, lab_page(page_marker='第1页 共2页')), store,
                      dispatch=dispatch) == 'SETTLED'
    assert [result.state for result in results] == [ExecutionState.RETRY_SCHEDULED]
    run = ProcessingRun.objects.get(document_id=continuation.pk)
    assert run.error_code == 'sampling_source_pending'
    assert not LabObservation.objects.exists()
    ProcessingRun.objects.filter(pk=run.pk).update(next_retry_at=timezone.now())
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    assert LabObservation.objects.count() == 1


@pytest.mark.parametrize('reviewed', ['', 'report', 'observation'])
def test_retry_after_indexing_failure_rebuilds_only_unreviewed_report_units(django_user_model, monkeypatch, reviewed):
    from datetime import timedelta
    from apps.labs.models import LabReportRevision, ObservationRevision
    from apps.processing.errors import RetryableProcessingError
    from apps.processing.models import ParsingVersion
    from apps.processing.runner import ProcessingContext

    _, patient = _patient(django_user_model, 'intake-indexing-retry-' + str(reviewed))
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    pipeline = _pipeline(store, lab_page())
    run_intake(item.pk, pipeline, store)
    item.refresh_from_db()
    first = ProcessingRun.objects.get(document=item.document)
    assert run_processing(first.pk, pipeline).state == ExecutionState.SUCCEEDED
    published = ParsingVersion.objects.get(processing_run=first)
    before = list(published.lab_report_units.values())
    original = store.objects[item.document.original_object_key]
    retry = ProcessingRun.objects.create(document=item.document, parser_version='indexing-retry', task_type='reparse',
        idempotency_key=f'{item.document_id}:indexing-retry', attempt_number=2)
    heartbeat = ProcessingContext.heartbeat

    def fail_indexing(context, stage):
        if stage == 'INDEXING':
            raise RetryableProcessingError('synthetic_indexing_failure')
        return heartbeat(context, stage)

    with monkeypatch.context() as patch:
        patch.setattr(ProcessingContext, 'heartbeat', fail_indexing)
        assert run_processing(retry.pk, pipeline).state == ExecutionState.RETRY_SCHEDULED
    building = ParsingVersion.objects.get(processing_run=retry)
    unit = building.lab_report_units.get()
    assert not building.active
    if reviewed == 'report':
        LabReportRevision.objects.create(unit=unit, author=patient.account, sequence=1, operation_id='existing-audit',
            before=unit.automatic, after=unit.automatic, source_evidence={}, rationale='保留已有核对历史')
    elif reviewed == 'observation':
        row = building.lab_observations.get()
        ObservationRevision.objects.create(observation=row, author=patient.account, sequence=1, origin='USER',
            action='CONFIRM', before={}, after={}, source_evidence=row.evidence)
    unpublished = list(building.lab_report_units.values())
    retry.refresh_from_db()
    result = run_processing(retry.pk, _pipeline(store, lab_page(time='2026-09-17 09:30')),
                            now=retry.next_retry_at + timedelta(seconds=1))
    assert result.state == (ExecutionState.FAILED if reviewed else ExecutionState.SUCCEEDED)
    assert list(published.lab_report_units.values()) == before
    assert store.objects[item.document.original_object_key] == original
    if reviewed:
        retry.refresh_from_db()
        assert retry.error_code == 'reviewed_unpublished_version_immutable'
        assert list(building.lab_report_units.values()) == unpublished
        assert LabReportRevision.objects.filter(unit=unit).count() == int(reviewed == 'report')
        assert ObservationRevision.objects.filter(observation__parsing_version=building).count() == int(reviewed == 'observation')
    else:
        assert building.lab_report_units.count() == 1
        assert not building.lab_report_units.filter(pk=unit.pk).exists()
        assert building.lab_report_units.get().automatic['sampled_at'] == '2026-09-17 09:30:00'


@pytest.mark.parametrize('changed', [False, True])
def test_actual_reparse_preserves_report_correction_without_rewriting_ocr(django_user_model, changed):
    from apps.labs.reports import correct_report, effective_report
    from apps.processing.models import ParsingVersion
    from tests.labs.test_report_revision_versions import SOURCE

    client, patient = _patient(django_user_model, 'intake-report-correction-' + str(changed))
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    pipeline = _pipeline(store, lab_page())
    run_intake(item.pk, pipeline, store)
    item.refresh_from_db()
    assert run_processing(ProcessingRun.objects.get(document=item.document).pk, pipeline).state == ExecutionState.SUCCEEDED
    original = LabReportUnit.objects.get(parsing_version__document=item.document)
    correct_report(patient, patient.account, original.pk, {'sampled_at': '2026-09-17 10:30'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='原件采样时间人工核对', operation_id='manual-time')
    retry = ProcessingRun.objects.create(document=item.document, parser_version='report-correction-reparse',
        task_type='reparse', idempotency_key=f'{item.document_id}:correction-reparse', attempt_number=2)
    assert run_processing(retry.pk, _pipeline(store, lab_page(report='NEW' if changed else 'A1'))).state == ExecutionState.SUCCEEDED
    current = ParsingVersion.objects.get(processing_run=retry).lab_report_units.get()
    identity = effective_report(current)
    assert identity.sampling_label == '2026-09-17 10:30'
    assert identity.status == ('REVIEW' if changed else 'ACCEPTED')
    assert current.automatic['sampled_at'] == '2026-09-17 08:30:00'
    assert original.revisions.count() == 1 and current.revisions.count() == 0
    content = client.get(f'/records/{item.document_id}/').content.decode()
    assert '原件采样时间人工核对' in content
    assert ('报告已接纳 0，待核对 1，拒收 0' if changed else '报告已接纳 1，待核对 0，拒收 0') in content


@pytest.mark.parametrize('new_time', ['2026-09-17', ''])
def test_reparse_missing_clock_excludes_results_despite_prior_manual_time(django_user_model, new_time):
    from copy import deepcopy
    from apps.exports.content import build_snapshot
    from apps.labs.readmodels import effective_rows
    from apps.labs.reports import correct_report
    from apps.labs.trends import trend_view
    from apps.patients.sharing_content import project_snapshot
    from apps.processing.models import ParsingVersion
    from tests.labs.test_report_revision_versions import SOURCE

    client, patient = _patient(django_user_model, 'reparse-missing-manual-clock-' + str(bool(new_time)))
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    pipeline = _pipeline(store, lab_page())
    run_intake(item.pk, pipeline, store)
    item.refresh_from_db()
    assert run_processing(ProcessingRun.objects.get(document=item.document).pk, pipeline).state == ExecutionState.SUCCEEDED
    original = LabReportUnit.objects.get(parsing_version__document=item.document)
    correct_report(patient, patient.account, original.pk, {'sampled_at': '2026-09-17 10:30'}, expected_revision=0,
                   source_evidence=SOURCE, rationale='保留原件采样时间核对历史', operation_id='manual-clock-before-reparse')
    revision = original.revisions.get()
    saved_revision, saved_automatic = deepcopy(revision.after), deepcopy(original.automatic)
    original_bytes = store.objects[item.document.original_object_key]
    retry = ProcessingRun.objects.create(document=item.document, parser_version='missing-clock-reparse',
        task_type='reparse', idempotency_key=f'{item.document_id}:missing-clock-reparse', attempt_number=2)
    assert run_processing(retry.pk, _pipeline(store, lab_page(time=new_time))).state == ExecutionState.NO_STRUCTURED_RESULT
    current = ParsingVersion.objects.get(processing_run=retry)
    assert current.active
    assert not current.lab_report_units.exists() and not current.lab_observations.exists()
    assert not current.ocr_blocks.exists()
    assert not effective_rows(patient)
    assert trend_view(patient, 'LAB_WBC', include_history=True) is None
    snapshot = build_snapshot(patient, {'mode': 'all', 'details': True})
    assert snapshot['labs'] == snapshot['lab_results'] == []
    shared = project_snapshot(snapshot, {'document_ids': [str(item.document_id)], 'sections': ['labs']})
    assert shared['labs'] == shared['lab_results'] == []
    original.refresh_from_db()
    revision.refresh_from_db()
    assert original.automatic == saved_automatic and revision.after == saved_revision
    assert original.observations.count() == 1 and original.revisions.count() == 1
    assert store.objects[item.document.original_object_key] == original_bytes
    response = client.get(f'/records/{item.document_id}/')
    assert response.status_code == 200
    content = response.content.decode()
    assert '报告已接纳 0，待核对 0，拒收 1' in content
    assert '保留原件采样时间核对历史' in content
    assert f'/records/{item.document_id}/viewer/' in content


@pytest.mark.parametrize('change', ['none', 'deleted', 'undone', 'conflicting_result', 'different_report', 'moved_result'])
def test_reprocessing_continuation_revalidates_current_main_evidence(django_user_model, change):
    from django.utils import timezone
    from apps.labs.reports import report_relations, decide_relation, effective_report
    from apps.processing.models import ParsingVersion

    _, patient = _patient(django_user_model, 'intake-reparse-' + change)
    store = InMemoryObjectStore()
    main, continuation, _ = admitted_continuation_pair(patient, store)
    original = store.objects[continuation.document.original_object_key]
    previous = ParsingVersion.objects.get(document_id=continuation.document_id, active=True)
    relation, = report_relations(patient)
    if change == 'deleted':
        Document.objects.filter(pk=main.document_id).update(deleted_at=timezone.now())
    elif change == 'undone':
        decide_relation(patient, patient.account, relation.pk, 'UNDO', expected_revision=relation.revision_number,
                        rationale='对照原件撤销', operation_id='reparse-undo')
    new_page = lab_page(time='', page_marker='第2页 共2页',
                        value='999' if change == 'conflicting_result' else '4.20',
                        report='OTHER' if change == 'different_report' else 'A1')
    if change == 'moved_result':
        new_page = replace(new_page, regions=tuple(replace(region,
            polygon=tuple((x, y + .01) for x, y in region.polygon)) if region.reading_order in {3, 4, 5, 6}
            else region for region in new_page.regions))
    next_run = ProcessingRun.objects.create(document_id=continuation.document_id, parser_version='reparse-v2',
        task_type='reparse', idempotency_key=f'{continuation.document_id}:reparse-v2', attempt_number=2)
    result = run_processing(next_run.pk, _pipeline(store, new_page))
    current = ParsingVersion.objects.get(processing_run=next_run)
    assert current.active
    assert store.objects[continuation.document.original_object_key] == original
    assert LabObservation.objects.filter(parsing_version=previous).count() == 1
    if change in {'none', 'moved_result'}:
        assert result.state == ExecutionState.SUCCEEDED
        row = LabObservation.objects.get(parsing_version=current)
        assert row.raw_value == '4.20'
        assert effective_report(row.report_unit).sampling_label == '2026-09-17 08:30'
        assert row.report_unit.automatic['time_source'].startswith(str(main.document_id))
        from apps.labs.readmodels import effective_rows
        visible = next(item for item in effective_rows(patient) if item.pk == row.pk)
        assert visible.report_identity.status == ('ACCEPTED' if change == 'none' else 'REVIEW')
        assert visible.report_identity.sampling_label == ('2026-09-17 08:30' if change == 'none' else '')
    else:
        assert result.state == ExecutionState.NO_STRUCTURED_RESULT
        assert not LabObservation.objects.filter(parsing_version=current).exists()


@pytest.mark.parametrize('decision,state', [('SAME', 'SAME'), ('DIFFERENT', 'DIFFERENT'), ('UNDO', 'UNDONE')])
def test_identical_reprocessing_preserves_manual_report_decision(django_user_model, decision, state):
    from apps.labs.reports import report_relations, decide_relation, ReportDecisionConflict

    _, patient = _patient(django_user_model, 'intake-reparse-decision-' + decision)
    store = InMemoryObjectStore()
    first, _ = stage(patient, store)
    second, _ = stage(patient, store, shade=10)
    pipeline = _pipeline(store, lab_page())
    for item in (first, second):
        assert run_intake(item.pk, pipeline, store) == 'SETTLED'
        item.refresh_from_db()
        assert run_processing(ProcessingRun.objects.get(document_id=item.document_id).pk, pipeline).state == ExecutionState.SUCCEEDED
    relation, = report_relations(patient)
    decide_relation(patient, patient.account, relation.pk, decision, expected_revision=relation.revision_number,
                    rationale='对照原件确认', operation_id='before-reparse')
    relation.refresh_from_db()
    previous_revision = relation.revision_number
    previous_events = list(relation.events.values())
    next_run = ProcessingRun.objects.create(document_id=second.document_id, parser_version='reparse-v2',
        task_type='reparse', idempotency_key=f'{second.document_id}:reparse-v2', attempt_number=2)
    assert run_processing(next_run.pk, pipeline).state == ExecutionState.SUCCEEDED
    refreshed, = report_relations(patient)
    assert refreshed.state == state
    assert refreshed.revision_number == previous_revision + 1
    assert list(refreshed.events.filter(sequence__lte=previous_revision).values()) == previous_events
    assert refreshed.events.order_by('-sequence').first().action == 'SOURCE_REFRESH'
    assert report_relations(patient)[0].revision_number == refreshed.revision_number
    with pytest.raises(ReportDecisionConflict):
        decide_relation(patient, patient.account, relation.pk, 'SAME', expected_revision=previous_revision,
                        rationale='过期页面', operation_id='stale-after-reparse')


def test_cleanup_failure_is_quarantined_and_retried(django_user_model):
    _, patient = _patient(django_user_model, 'intake-cleanup')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    store.fail_delete = True
    run_intake(item.pk, _pipeline(store, lab_page(time='2026-09-17')), store)
    intake = UploadIntake.objects.get(item=item)
    assert intake.cleanup_pending and intake.recognition == {}
    assert not Document.objects.exists()
    store.fail_delete = False
    assert cleanup_intake(item.pk, store)
    intake.refresh_from_db()
    assert not intake.cleanup_pending and intake.staged == {} and store.objects == {}


def test_non_laboratory_document_does_not_need_sampling_time(django_user_model):
    _, patient = _patient(django_user_model, 'intake-nonlab')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    original = _ocr_page()
    page = replace(original, regions=(_region('影像报告', .05, .9, .04, 1),))
    assert run_intake(item.pk, _pipeline(store, page), store) == 'SETTLED'
    item.refresh_from_db()
    assert item.document_id and item.validity['status'] == 'ACCEPTED'


def test_exact_file_dedup_is_patient_scoped_and_skips_recognition(django_user_model):
    _, patient = _patient(django_user_model, 'intake-dedup')
    _, other = _patient(django_user_model, 'intake-other')
    store = InMemoryObjectStore()
    first, _ = stage(patient, store)
    run_intake(first.pk, _pipeline(store, lab_page()), store)
    duplicate, outcome = stage(patient, store)
    assert outcome.kind.value == 'EXACT_DUPLICATE'
    assert not UploadIntake.objects.filter(item=duplicate).exists()
    _, other_outcome = stage(other, store)
    assert other_outcome.kind.value == 'VALIDATING' and other_outcome.document_id is None


@pytest.mark.parametrize('with_titles', [True, False])
def test_mixed_image_preserves_original_but_excludes_rejected_report_everywhere(django_user_model, with_titles):
    _, patient = _patient(django_user_model, 'intake-mixed')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    original_bytes = next(iter(store.objects.values()))
    regions = []
    for index, page in enumerate((lab_page(), lab_page(time='2026-09-17', report='REJECTED99', value='999.99'))):
        regions.extend(replace(region, reading_order=region.reading_order + index * 10,
            polygon=tuple((x, y / 2 + index / 2) for x, y in region.polygon)) for region in page.regions)
    mixed = replace(lab_page(), regions=tuple(regions))
    if not with_titles:
        mixed = replace(mixed, regions=tuple(replace(region, text=region.text.replace(' 检验报告', '')) for region in mixed.regions))
    pipeline = _pipeline(store, mixed)
    assert run_intake(item.pk, pipeline, store) == 'SETTLED'
    item.refresh_from_db()
    assert item.validity['accepted'] == item.validity['rejected'] == 1
    assert item.validity['shared_original_retained']
    assert store.objects[item.document.original_object_key] == original_bytes
    assert run_processing(ProcessingRun.objects.get(document=item.document).pk, pipeline).state == ExecutionState.SUCCEEDED
    assert list(LabObservation.objects.values_list('raw_value', flat=True)) == ['4.20']
    assert LabReportUnit.objects.count() == 1
    assert not OcrBlock.objects.filter(text__contains='999.99').exists()
    assert not OcrBlock.objects.filter(text__contains='REJECTED99').exists()
    assert 'REJECTED99' not in str(UploadIntake.objects.get(item=item).recognition)


def test_conflicting_overlap_cannot_supply_continuation_sampling_time(django_user_model):
    _, patient = _patient(django_user_model, 'intake-conflict')
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, file_count=2, created_by=patient.account)
    first, _ = stage(patient, store, batch=batch)
    second, _ = stage(patient, store, batch=batch, ordinal=2, shade=10)
    run_intake(first.pk, _pipeline(store, lab_page(page_marker='第1页 共2页')), store)
    run_intake(second.pk, _pipeline(store, lab_page(time='', page_marker='第2页 共2页', value='6.20')), store)
    second.refresh_from_db()
    assert second.status == 'REJECTED' and not second.document_id
    assert Document.objects.count() == 1


def test_upload_http_returns_validation_before_archive_then_projects_mixed_decisions(django_user_model, monkeypatch):
    from tests.documents.test_upload_views import authenticated_client, reserve_one, upload_path

    client, patient = authenticated_client(django_user_model)
    store = InMemoryObjectStore()
    monkeypatch.setattr('apps.documents.views.uploads.get_object_store', lambda: store)
    batch_id, item_id = reserve_one(client)
    from django.core.files.uploadedfile import SimpleUploadedFile
    from tests.processing.test_pipeline import _png_bytes
    response = client.post(upload_path(batch_id, item_id), {'file': SimpleUploadedFile('report.png', _png_bytes())})
    assert response.status_code == 202
    assert response.json()['status'] == 'VALIDATING'
    assert response.json()['document_id'] is None and not response.json()['saved']
    assert not Document.objects.exists()
    assert run_intake(item_id, _pipeline(store, lab_page(time='2026-09-17')), store) == 'SETTLED'
    payload = client.get(f'/api/upload-batches/{batch_id}/status/').json()
    assert payload['terminal'] and payload['counts']['rejected'] == 1
    assert payload['items'][0]['status'] == 'REJECTED'
    assert payload['items'][0]['validity']['units'][0]['reason'] == 'sampling_time_missing'


def test_mixed_pdf_preserves_every_original_page_but_only_extracts_valid_page(django_user_model):
    _, patient = _patient(django_user_model, 'intake-pdf')
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, created_by=patient.account, file_count=1)
    item = UploadItem.objects.create(batch=batch, ordinal=1, display_filename='mixed.pdf', status='UPLOADING')
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    pdf.write(buffer)
    payload = buffer.getvalue()
    with inspect_upload(io.BytesIO(payload), item.display_filename) as inspected:
        with inspected.open() as source:
            staged = store.put_staging(source, expected_size=inspected.byte_size, expected_sha256=inspected.sha256)
        register_intake(patient, batch.pk, item.pk, inspected, staged, store, actor=patient.account)
    # PDF raster preparation determines pixel dimensions; this provider preserves
    # its geometry while supplying synthetic report text on each actual page.
    class PdfProvider:
        name, version = 'fixture', '1.0'

        def recognize(self, page):
            unit = lab_page() if page.page_number == 1 else lab_page(time='2026-09-17', report='BAD-PDF', value='999.99')
            return replace(unit, page_number=page.page_number, width=page.width, height=page.height)

    pipeline = _pipeline(store, lab_page())
    pipeline.raster_provider = PdfProvider()
    assert run_intake(item.pk, pipeline, store) == 'SETTLED'
    item.refresh_from_db()
    assert item.validity['shared_original_retained'] and item.validity['rejected'] == 1
    assert store.objects[item.document.original_object_key] == payload
    assert item.document.page_count == 2
    assert run_processing(ProcessingRun.objects.get(document=item.document).pk, pipeline).state == ExecutionState.SUCCEEDED
    assert list(LabObservation.objects.values_list('raw_value', flat=True)) == ['4.20']
    assert not OcrBlock.objects.filter(document_page__page_number=2).exists()


@pytest.mark.parametrize('new_time,revoke_second', [('2026-09-17 08:30', False),
    ('2026-09-17 10:30', False), ('2026-09-17 08:30', True)])
def test_pdf_reprocessing_continuation_uses_new_main_page_evidence(django_user_model, new_time, revoke_second):
    from apps.labs.readmodels import effective_rows
    from apps.labs.reports import effective_report, report_relations, decide_relation
    from apps.processing.models import ParsingVersion

    _, patient = _patient(django_user_model, 'intake-pdf-reparse')
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, created_by=patient.account, file_count=1)
    item = UploadItem.objects.create(batch=batch, ordinal=1, display_filename='continued.pdf', status='UPLOADING')
    page_count = 3 if revoke_second else 2
    pdf = PdfWriter()
    for _ in range(page_count):
        pdf.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    pdf.write(buffer)
    payload = buffer.getvalue()
    with inspect_upload(io.BytesIO(payload), item.display_filename) as inspected:
        with inspected.open() as source:
            staged = store.put_staging(source, expected_size=inspected.byte_size, expected_sha256=inspected.sha256)
        register_intake(patient, batch.pk, item.pk, inspected, staged, store, actor=patient.account)

    class PdfProvider:
        name, version = 'fixture', '1.0'
        main_time = '2026-09-17 08:30'

        def recognize(self, page):
            unit = lab_page(time=self.main_time if page.page_number == 1 else '',
                            page_marker=f'第{page.page_number}页 共{page_count}页')
            return replace(unit, page_number=page.page_number, width=page.width, height=page.height)

    pipeline = _pipeline(store, lab_page())
    pipeline.raster_provider = PdfProvider()
    assert run_intake(item.pk, pipeline, store) == 'SETTLED'
    item.refresh_from_db()
    assert run_processing(ProcessingRun.objects.get(document=item.document).pk, pipeline).state == ExecutionState.SUCCEEDED
    relations = report_relations(patient)
    if revoke_second:
        relation = next(item_relation for item_relation in relations if
            {item_relation.left_key, item_relation.right_key} == {f'{item.document_id}:1:1', f'{item.document_id}:2:1'})
        decide_relation(patient, patient.account, relation.pk, 'UNDO', expected_revision=relation.revision_number,
                        rationale='撤销第二页关联', operation_id='revoke-second-page')
    pipeline.raster_provider.main_time = new_time
    next_run = ProcessingRun.objects.create(document_id=item.document_id, parser_version='reparse-v2',
        task_type='reparse', idempotency_key=f'{item.document_id}:reparse-v2', attempt_number=2)
    assert run_processing(next_run.pk, pipeline).state == ExecutionState.SUCCEEDED
    current = ParsingVersion.objects.get(processing_run=next_run)
    assert current.lab_observations.count() == 2
    continuation_page = 3 if revoke_second else 2
    if revoke_second:
        assert not current.lab_observations.filter(document_page__page_number=2).exists()
        relation.refresh_from_db()
        assert relation.state == 'UNDONE'
    continued = current.lab_report_units.get(document_page__page_number=continuation_page)
    assert effective_report(continued).sampling_label == new_time
    assert store.objects[item.document.original_object_key] == payload
    projected = next(row for row in effective_rows(patient) if row.document_page.page_number == continuation_page)
    assert projected.report_identity.status == ('ACCEPTED' if new_time.endswith('08:30') else 'REVIEW')
    assert projected.report_identity.sampling_label == (new_time if new_time.endswith('08:30') else '')


def test_pending_upload_reserves_quota_before_any_document_exists(django_user_model):
    from apps.documents.models import PatientUploadQuota
    from apps.documents.quotas import QuotaExceeded

    _, patient = _patient(django_user_model, 'intake-quota')
    PatientUploadQuota.objects.create(patient=patient, document_limit=1)
    store = InMemoryObjectStore()
    stage(patient, store)
    with pytest.raises(QuotaExceeded, match='document_limit'):
        stage(patient, store, shade=10)
    assert not Document.objects.exists()


def test_deleted_patient_cannot_leave_temporary_ocr_or_staging(django_user_model, monkeypatch):
    from apps.patients.deletion import request_patient_deletion, purge_patient_deletions
    from apps.patients.models import Patient

    _, patient = _patient(django_user_model, 'intake-deletion')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    request_patient_deletion(patient.pk, patient.account, document_dispatch=lambda identity: None)
    assert run_intake(item.pk, _pipeline(store, lab_page()), store) == 'SETTLED'
    assert not Document.objects.exists() and store.objects == {}
    monkeypatch.setattr('apps.documents.backends.get_object_store', lambda: store)
    assert purge_patient_deletions(patient_ids=[patient.pk]) == 1
    assert not Patient.objects.filter(pk=patient.pk).exists()


def test_titleless_laboratory_screenshot_cannot_bypass_time_requirement(django_user_model):
    _, patient = _patient(django_user_model, 'intake-no-title')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    original = lab_page(time='2026-09-17')
    page = replace(original, regions=tuple(region for region in original.regions if region.reading_order != 1))
    assert run_intake(item.pk, _pipeline(store, page), store) == 'SETTLED'
    item.refresh_from_db()
    assert item.status == 'REJECTED' and not Document.objects.exists()


def test_recognition_failure_is_retryable_then_failed_never_medically_rejected(django_user_model):
    from django.utils import timezone

    _, patient = _patient(django_user_model, 'intake-retry')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    pipeline = _pipeline(store, lab_page())
    pipeline.raster_provider = None
    for _ in range(3):
        assert run_intake(item.pk, pipeline, store) == 'RETRY_SCHEDULED'
        UploadIntake.objects.filter(item=item).update(next_attempt_at=timezone.now())
    assert run_intake(item.pk, pipeline, store) == 'SETTLED'
    item.refresh_from_db()
    assert item.status == 'UPLOAD_FAILED' and item.error_code == 'upload_service_unavailable'
    assert not Document.objects.exists() and not store.objects


def test_repeated_delivery_and_same_batch_exact_duplicate_do_not_create_extra_originals(django_user_model):
    _, patient = _patient(django_user_model, 'intake-idempotency')
    store = InMemoryObjectStore()
    batch = UploadBatch.objects.create(patient=patient, created_by=patient.account, file_count=2)
    first, _ = stage(patient, store, batch=batch)
    second, _ = stage(patient, store, batch=batch, ordinal=2)
    pipeline = _pipeline(store, lab_page())
    assert run_intake(first.pk, pipeline, store) == 'WAITING_BATCH'
    assert run_intake(second.pk, pipeline, store) == 'SETTLED'
    assert run_intake(second.pk, pipeline, store) == 'SETTLED'
    assert Document.objects.count() == ProcessingRun.objects.count() == 1
    assert sorted(batch.items.values_list('status', flat=True)) == ['CREATED', 'EXACT_DUPLICATE']
    assert len(store.objects) == 1
    assert UploadIntake.objects.get(item__status='EXACT_DUPLICATE').recognition == {}


def test_expired_recognition_lease_recovers_without_admitting_stale_result(django_user_model):
    import uuid
    from datetime import timedelta
    from django.utils import timezone
    from apps.documents.intake import recover_intakes, _RecognitionContext
    from apps.documents.services import UploadStateConflict

    _, patient = _patient(django_user_model, 'intake-lease')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    old_token = uuid.uuid4()
    UploadIntake.objects.filter(item=item).update(state='RECOGNIZING', lease_token=old_token,
        heartbeat_at=timezone.now() - timedelta(minutes=16))
    assert recover_intakes(store=store) == (item.pk,)
    with pytest.raises(UploadStateConflict):
        _RecognitionContext(item.pk, old_token).heartbeat()
    assert run_intake(item.pk, _pipeline(store, lab_page()), store) == 'SETTLED'
    assert Document.objects.count() == 1


@pytest.mark.parametrize('delete_patient', [False, True])
def test_admission_resumes_after_process_loss_between_promotion_and_database_commit(django_user_model, monkeypatch, delete_patient):
    _, patient = _patient(django_user_model, 'intake-promotion-retry')
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    pipeline = _pipeline(store, lab_page())
    original = store.promote_immutable

    class WorkerLost(BaseException):
        pass

    def lose_worker(staged, key, **kwargs):
        original(staged, key, **kwargs)
        raise WorkerLost()

    with monkeypatch.context() as patch:
        patch.setattr(store, 'promote_immutable', lose_worker)
        with pytest.raises(WorkerLost):
            run_intake(item.pk, pipeline, store)
    assert not Document.objects.exists()
    assert UploadIntake.objects.get(item=item).state == 'READY'
    if delete_patient:
        from apps.patients.deletion import request_patient_deletion
        request_patient_deletion(patient.pk, patient.account, document_dispatch=lambda identity: None)
    assert run_intake(item.pk, pipeline, store) == 'SETTLED'
    assert Document.objects.count() == ProcessingRun.objects.count() == len(store.objects) == (0 if delete_patient else 1)


def test_failed_upload_cannot_discard_cleanup_job_by_retry_or_remove(django_user_model):
    from apps.documents.intake import _cancel_intake
    from apps.documents.services import UploadStateConflict
    from apps.documents.views.uploads import _begin_upload

    client, patient = _patient(django_user_model, 'intake-cleanup-boundary')
    account = patient.account
    store = InMemoryObjectStore()
    item, _ = stage(patient, store)
    store.fail_delete = True
    _cancel_intake(UploadIntake.objects.get(item=item), store, 'upload_service_unavailable')
    with pytest.raises(UploadStateConflict):
        _begin_upload(patient, item.batch_id, item.pk, account)
    item.refresh_from_db()
    assert item.status == 'UPLOAD_FAILED'
    response = client.post(f'/api/upload-batches/{item.batch_id}/items/{item.pk}/remove/')
    assert response.status_code == 409
    assert UploadIntake.objects.filter(item=item, cleanup_pending=True).exists()
    store.fail_delete = False
    assert cleanup_intake(item.pk, store)
    assert _begin_upload(patient, item.batch_id, item.pk, account).status == 'UPLOADING'
