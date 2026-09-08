"""All scanned slots and actual typed-parent availability stay in private input."""
import hashlib

import pytest

from apps.cancer_ordering.models import CancerCandidate, CollectionRun
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current, OrderingConflict
from apps.facts.clinical_readmodels import report_source_token
from apps.facts.clinical_services import create_manual_report, add_manual_clinical_field
from apps.facts.models import ClinicalReport, Fact
from apps.patients.models import PatientMembership
from apps.processing.runner import run_processing, ExecutionState
from tests.cancer_ordering.test_narrative_dependencies import current_row
from tests.cancer_ordering.test_pipeline import _page
from tests.cancer_ordering.test_services import _revise
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.processing.test_pipeline import _document_and_run, _pipeline, _png_bytes, _Store


pytestmark = pytest.mark.django_db(transaction=True)


def test_zero_candidate_inventory_preserves_exact_scanned_slot_positions(django_user_model):
    _, patient = _patient(django_user_model, 'narrative-empty-positions')
    _, version = parsed_facts(patient, ['主诉：头痛。'], document_type='UNKNOWN')
    collect_current(patient, actor=patient.account)
    run = CollectionRun.objects.get(parsing_version=version)
    slot, = run.input_snapshot['narratives']['slots']
    assert slot['role'] == 'CHIEF_COMPLAINT' and slot['text'] == '主诉：头痛。'
    block = version.ocr_blocks.get()
    assert slot['character_map'] == [[str(block.pk), offset] for offset in range(len(block.text))]
    assert slot['body_start'] == 3 and run.candidate_count == 0


def test_failed_structured_parent_extraction_cannot_be_claimed_complete_by_narrative_worker(django_user_model, monkeypatch):
    from apps.facts import clinical_extraction
    document, run = _document_and_run(django_user_model)
    def fail(_):
        raise ValueError('synthetic private extraction context')
    monkeypatch.setattr(clinical_extraction, 'extract_clinical_version', fail)
    result = run_processing(run.pk, _pipeline(_Store(_png_bytes()), _page('主诉：肺癌。')))
    assert result.state == ExecutionState.SUCCEEDED
    receipt = CollectionRun.objects.get(document=document)
    assert receipt.status == 'FAILED'
    assert receipt.input_snapshot['narratives']['clinical_extraction']['status'] == 'FAILED'
    assert 'synthetic private extraction context' not in str(receipt.input_snapshot)
    assert document.parsing_versions.get(active=True).ocr_blocks.count() == 2
    assert resolve_ordering(document.patient)['profile'] == 'GENERAL'


def manual_parent(model, name):
    _, patient = _patient(model, name)
    document, version = parsed_facts(patient, ['主诉：肺癌。'], document_type='UNKNOWN')
    author = model.objects.create(phone_hash=hashlib.sha256((name + '-author').encode()).hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=author, role='EDITOR')
    report = create_manual_report(patient, actor=author, document_id=document.pk,
        spans=[{'page_number': 1}], title='合成来源报告', expected_lifecycle_revision=document.lifecycle_revision,
        expected_version_id=str(version.pk))
    field = add_manual_clinical_field(patient, actor=author, report_id=report.pk, entity_key='report',
        field_key='imaging.impression', value={'text': '肺癌'}, fragments=[{'page_number': 1, 'raw_text': '肺癌'}],
        expected_report_source=report_source_token(report))
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.get(patient=patient).source_narrative_id
    return patient, author, report, field


def test_purged_manual_field_and_report_authors_are_unavailable_parents(django_user_model):
    patient, author, _, _ = manual_parent(django_user_model, 'narrative-typed-author')
    _revise(patient, current_row(patient), 'CONFIRM', checked_original=True)
    assert resolve_ordering(patient)['profile'] == 'LUNG'
    author.delete()
    assert not current_row(patient)['source_valid']
    with pytest.raises(OrderingConflict):
        _revise(patient, current_row(patient), 'CONFIRM', checked_original=True)


def test_all_parent_boundary_inputs_invalidate_the_collection_before_materialization(django_user_model):
    patient, _, report, _ = manual_parent(django_user_model, 'narrative-typed-input')
    before = resolve_ordering(patient)
    assert before['complete']
    ClinicalReport.objects.filter(pk=report.pk).update(source_fingerprint='f' * 64)
    after = resolve_ordering(patient)
    assert not after['complete'] and after['fingerprint'] != before['fingerprint']
