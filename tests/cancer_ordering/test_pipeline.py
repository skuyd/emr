from copy import deepcopy
import uuid

from django.db import transaction
from django.utils import timezone
import pytest

from apps.cancer_ordering import matching
from apps.cancer_ordering.extraction import collect_processing_version
from apps.cancer_ordering.models import CancerCandidate, CandidateRevision, CollectionRun, DisplaySelection
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.cancer_ordering.sources import SourceContext
from apps.documents.models import ProcessingRun
from apps.facts.models import Fact, FactExtraction, FactRevision
from apps.facts.revisions import revise_fact
from apps.patients.models import PatientMembership
from apps.processing.errors import ProcessingContractError, ProcessingLeaseLost
from apps.processing.models import OcrBlock, ParsingVersion
from apps.processing.runner import ExecutionState, _acquire, run_processing
from apps.processing.value_objects import OcrPage
from tests.facts.factories import parsed_facts
from tests.processing.test_pipeline import _document_and_run, _pipeline, _png_bytes, _region, _Store


pytestmark = pytest.mark.django_db(transaction=True)


def _worker_case(django_user_model):
    document, run = _document_and_run(django_user_model)
    assert PatientMembership.objects.get(patient=document.patient, account=document.patient.account).role == 'ADMIN'
    return document, run


def _page(*lines):
    return OcrPage(1, 100, 100, tuple(_region(text, .05, .90, .05 + index * .1, index)
                   for index, text in enumerate(('合成出院小结', *lines))), 'fixture', '1.0')


def test_actual_worker_collects_before_publication_and_default_auto_works_afterward(django_user_model):
    document, run = _worker_case(django_user_model)
    text = '病理诊断：右肺上叶浸润性腺癌 pT2aN1M0 ⅢA期。'
    store = _Store(_png_bytes())
    result = run_processing(run.pk, _pipeline(store, _page(text)))
    assert result.state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.status == 'PUBLISHED'
    receipt = CollectionRun.objects.filter(parsing_version=version).first()
    assert receipt is not None and receipt.status == 'COMPLETE' and receipt.candidate_count == 1
    row = CancerCandidate.objects.get(patient=document.patient)
    assert row.created_by_id is None and row.original_data['label'] == '右肺上叶浸润性腺癌'
    assert row.original_source['binding_kind'] == 'OCR'
    assert resolve_ordering(document.patient)['profile'] == 'LUNG'
    assert not CandidateRevision.objects.exists() and not FactRevision.objects.exists()
    assert not DisplaySelection.objects.exists() and store.keys == [document.original_object_key]


def test_successful_zero_candidate_scope_is_recorded_by_the_real_worker(django_user_model):
    document, run = _worker_case(django_user_model)
    result = run_processing(run.pk, _pipeline(_Store(_png_bytes()), _page('本页未列出明确诊断栏目。')))
    assert result.state == ExecutionState.SUCCEEDED
    receipt = CollectionRun.objects.filter(document=document).first()
    assert receipt is not None and receipt.status == 'COMPLETE' and receipt.candidate_count == 0
    current = resolve_ordering(document.patient)
    assert current['complete'] and current['profile'] == 'GENERAL'


def test_fact_extraction_failure_still_archives_ocr_and_records_incomplete_scope(django_user_model, monkeypatch):
    from apps.processing import pipeline

    document, run = _worker_case(django_user_model)
    def fail(_):
        raise ValueError('synthetic private source failure')
    monkeypatch.setattr(pipeline, 'extract_version_facts', fail)
    assert run_processing(run.pk, _pipeline(_Store(_png_bytes()), _page('出院诊断：肺癌。'))).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and OcrBlock.objects.filter(parsing_version=version).count() == 2
    assert FactExtraction.objects.get(parsing_version=version).status == 'FAILED'
    receipt = CollectionRun.objects.filter(parsing_version=version).first()
    assert receipt is not None and receipt.status == 'FAILED' and receipt.error_code == 'fact_extraction_incomplete'
    assert not resolve_ordering(document.patient)['complete']


def test_collector_failure_rolls_back_only_its_candidates_and_write_retry_preserves_attempt(django_user_model, monkeypatch):
    document, run = _worker_case(django_user_model)
    original = matching.literal_candidates
    def fail_second(text, category):
        if category == 'PATHOLOGY':
            raise ValueError('synthetic private second section')
        return original(text, category)
    with monkeypatch.context() as patch:
        patch.setattr(matching, 'literal_candidates', fail_second)
        result = run_processing(run.pk, _pipeline(_Store(_png_bytes()), _page('出院诊断：肺癌。', '病理诊断：胰腺癌。')))
    assert result.state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert Fact.objects.filter(parsing_version=version, representation='EXCERPT').count() == 2
    failed = CollectionRun.objects.filter(parsing_version=version).first()
    assert failed is not None and failed.status == 'FAILED' and failed.error_code == 'candidate_collection_failed'
    before = deepcopy(failed.input_snapshot)
    assert not CancerCandidate.objects.exists() and not resolve_ordering(document.patient)['complete']
    collect_current(document.patient, actor=document.patient.account)
    failed.refresh_from_db()
    assert failed.status == 'FAILED' and failed.input_snapshot == before
    assert list(CollectionRun.objects.order_by('sequence').values_list('status', flat=True)) == ['FAILED', 'COMPLETE']
    assert CancerCandidate.objects.count() == 2 and resolve_ordering(document.patient)['complete']


def test_source_capture_exception_retains_original_and_anonymous_failed_receipt(django_user_model, monkeypatch):
    document, run = _worker_case(django_user_model)
    def fail(*args, **kwargs):
        raise ValueError('synthetic source detail must not appear in persisted errors')
    with monkeypatch.context() as patch:
        patch.setattr(SourceContext, 'scopes', fail)
        result = run_processing(run.pk, _pipeline(_Store(_png_bytes()), _page('出院诊断：肺癌。')))
    assert result.state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    receipt = CollectionRun.objects.filter(parsing_version=version).first()
    assert receipt is not None and receipt.status == 'FAILED' and receipt.error_code == 'candidate_source_capture_failed'
    assert 'synthetic source detail' not in str(receipt.input_snapshot)
    assert version.active and Fact.objects.filter(parsing_version=version).exists()
    assert resolve_ordering(document.patient)['profile'] == 'GENERAL'
    collect_current(document.patient, actor=document.patient.account)
    assert resolve_ordering(document.patient)['profile'] == 'LUNG'


def test_collector_rejects_a_replaced_actual_worker_lease(django_user_model):
    document, run = _worker_case(django_user_model)
    context = _acquire(run.pk, timezone.now)
    _pipeline(_Store(_png_bytes()), _page('出院诊断：肺癌。')).run(context)
    version = ParsingVersion.objects.get(processing_run=run)
    prior = CollectionRun.objects.count()
    ProcessingRun.objects.filter(pk=run.pk).update(lease_token=uuid.uuid4())
    with transaction.atomic(), pytest.raises(ProcessingLeaseLost):
        collect_processing_version(context, version)
    assert CollectionRun.objects.count() == prior and not version.active


def test_collector_requires_the_current_transaction_and_matching_run_version(django_user_model):
    document, run = _worker_case(django_user_model)
    context = _acquire(run.pk, timezone.now)
    _, other = parsed_facts(document.patient, ['出院诊断：胰腺癌。'])
    with pytest.raises(ProcessingContractError):
        collect_processing_version(context, other)
    with transaction.atomic(), pytest.raises(ProcessingContractError):
        collect_processing_version(context, other)
    assert not CollectionRun.objects.exists()


@pytest.mark.parametrize('parent_action', [None, 'CONFIRM', 'CORRECT'])
def test_identical_reparse_captures_real_inherited_source_before_publication(django_user_model, parent_action):
    document, first = _worker_case(django_user_model)
    pipeline = _pipeline(_Store(_png_bytes()), _page('出院诊断：肺癌。'))
    assert run_processing(first.pk, pipeline).state == ExecutionState.SUCCEEDED
    first_version = ParsingVersion.objects.get(processing_run=first)
    old_fact = Fact.objects.get(parsing_version=first_version, representation='EXCERPT')
    if parent_action:
        revise_fact(document.patient, old_fact.pk, actor=document.patient.account, action=parent_action,
                    expected_revision=0, checked_original=True,
                    changes={'text': '出院诊断：胰腺癌。'} if parent_action == 'CORRECT' else None)
    second = ProcessingRun.objects.create(document=document, parser_version='parser-v2', task_type='reparse',
        attempt_number=2, idempotency_key=str(uuid.uuid4()))
    assert run_processing(second.pk, pipeline).state == ExecutionState.SUCCEEDED
    state = resolve_ordering(document.patient)
    assert state['complete'] is True
    current = ParsingVersion.objects.get(processing_run=second)
    assert current.previous_version_id == first_version.pk
    candidate = CancerCandidate.objects.get(source_fact__parsing_version=current)
    assert candidate.original_data['profile'] == ('PANCREAS' if parent_action == 'CORRECT' else 'LUNG')
    assert state['profile'] == ('GENERAL' if parent_action == 'CORRECT' else 'LUNG')
    assert CandidateRevision.objects.count() == 0
    old_fact.refresh_from_db()
    assert old_fact.raw_text == '出院诊断：肺癌。'
