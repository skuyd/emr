"""Actual archive/collection outcomes for the independently bounded narrative input."""
from copy import deepcopy

import pytest

from apps.cancer_ordering.models import CancerCandidate, CollectionRun
from apps.cancer_ordering.readmodels import candidate_rows, resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.processing.models import OcrBlock, ParsingVersion
from apps.processing.runner import ExecutionState, run_processing
from tests.cancer_ordering.test_pipeline import _page
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.processing.test_pipeline import _document_and_run, _pipeline, _png_bytes, _Store


pytestmark = pytest.mark.django_db(transaction=True)


def test_real_worker_collects_a_headed_narrative_without_creating_a_fact(django_user_model):
    document, run = _document_and_run(django_user_model)
    result = run_processing(run.pk, _pipeline(_Store(_png_bytes()), _page('主诉：肺癌治疗后不适。')))
    assert result.state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.status == 'PUBLISHED'
    assert Fact.objects.filter(parsing_version=version).count() == 0
    candidate = CancerCandidate.objects.filter(patient=document.patient).first()
    assert candidate is not None, 'A headed actual OCR source must not depend on an excerpt Fact existing'
    assert candidate.source_fact_id is None and candidate.source_report_id is None
    assert candidate.source_narrative.document_id == document.pk
    assert candidate.source_narrative.document_page_id == document.pages.get().pk
    state = resolve_ordering(document.patient)
    assert state['complete'] and state['profile'] == 'LUNG'
    assert not candidate.revisions.exists()
    assert CollectionRun.objects.get(parsing_version=version).candidate_count == 1


def test_recollect_is_idempotent_and_keeps_actual_treatment_excerpt_unchanged(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-collect-parent')
    _, version = parsed_facts(patient, ['现病史：患者诊断为肺癌，已行化疗。'])
    fact = Fact.objects.get(parsing_version=version)
    assert fact.category == 'TREATMENT'
    original = deepcopy({key: getattr(fact, key) for key in ('raw_text', 'reading_order', 'automatic_content', 'revision_number')})
    blocks = list(OcrBlock.objects.filter(parsing_version=version).values())
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.filter(patient=patient).count() == 1
    first = CancerCandidate.objects.get(patient=patient)
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.get(patient=patient).pk == first.pk
    assert CollectionRun.objects.filter(patient=patient).count() == 1
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    fact.refresh_from_db()
    assert {key: getattr(fact, key) for key in original} == original
    assert Fact.objects.filter(parsing_version=version).count() == 1 and not fact.revisions.exists()
    assert list(OcrBlock.objects.filter(parsing_version=version).values()) == blocks


def test_new_unmaterialized_narrative_invalidates_a_previously_complete_auto(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-new-input')
    parsed_facts(patient, ['出院诊断：肺癌。'])
    collect_current(patient, actor=patient.account)
    before = resolve_ordering(patient)
    assert before['complete'] and before['profile'] == 'LUNG'
    parsed_facts(patient, ['主诉：胰腺癌。'], document_type='UNKNOWN')
    after = resolve_ordering(patient)
    assert not after['complete'] and after['profile'] == 'GENERAL'
    assert after['fingerprint'] != before['fingerprint']
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['reason'] == 'reported_diagnoses_differ'


def test_headed_zero_candidate_scope_keeps_its_page_inventory(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-zero')
    _, version = parsed_facts(patient, ['主诉：头痛。'], document_type='UNKNOWN')
    collect_current(patient, actor=patient.account)
    run = CollectionRun.objects.filter(parsing_version=version).first()
    assert run is not None and run.status == 'COMPLETE' and run.candidate_count == 0
    coverage = run.input_snapshot['narratives']['coverage']
    assert len(coverage) == 1 and coverage[0]['status'] == 'SCOPED' and coverage[0]['slot_count'] == 1


def test_repeated_original_literals_remain_distinct_source_occurrences(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-repeat')
    parsed_facts(patient, ['主诉：肺癌；肺癌。'], document_type='UNKNOWN')
    collect_current(patient, actor=patient.account)
    rows = list(CancerCandidate.objects.filter(patient=patient))
    assert len(rows) == 2
    assert len({row.occurrence_key for row in rows}) == 2
    assert rows[0].original_source['label_fragments'] != rows[1].original_source['label_fragments']


def test_existing_diagnosis_fact_route_is_preserved_with_its_exclusion(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-old-route')
    _, version = parsed_facts(patient, ['临床诊断：肺癌。'])
    fact = Fact.objects.get(parsing_version=version)
    revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
    collect_current(patient, actor=patient.account)
    row, = candidate_rows(patient)
    assert not row['source_valid'] and resolve_ordering(patient)['profile'] == 'GENERAL'
    assert CancerCandidate.objects.get(patient=patient).source_fact_id == fact.pk


def test_narrative_capture_failure_retains_anonymous_attempt_and_write_retry(django_user_model, monkeypatch):
    from apps.cancer_ordering import narrative_matching
    from apps.cancer_ordering.models import NarrativeSource
    document, run = _document_and_run(django_user_model)
    def fail(_):
        raise ValueError('synthetic private narrative source failure')
    with monkeypatch.context() as patch:
        patch.setattr(narrative_matching, 'narrative_candidates', fail)
        result = run_processing(run.pk, _pipeline(_Store(_png_bytes()), _page('主诉：肺癌。')))
    assert result.state == ExecutionState.SUCCEEDED
    failed = CollectionRun.objects.get(document=document)
    assert failed.status == 'FAILED' and failed.error_code == 'candidate_source_capture_failed'
    original = deepcopy(failed.input_snapshot)
    assert 'synthetic private narrative source failure' not in str(original)
    assert not NarrativeSource.objects.exists() and not Fact.objects.filter(document=document).exists()
    collect_current(document.patient, actor=document.patient.account)
    assert resolve_ordering(document.patient)['profile'] == 'LUNG'
    assert list(CollectionRun.objects.filter(document=document).order_by('sequence').values_list('status', flat=True)) == ['FAILED', 'COMPLETE']
    failed.refresh_from_db()
    assert failed.input_snapshot == original and failed.status == 'FAILED'
