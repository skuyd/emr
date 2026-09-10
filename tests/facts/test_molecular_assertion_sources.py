"""Actual OCR fragments distinguish the field's own value from copied proof."""
import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_context import validate_context_candidate
from apps.facts.clinical_schema import field_content
from apps.facts.clinical_services import create_manual_report
from apps.facts.models import Fact, FactSourceFragment
from apps.processing.models import SourceEvidence
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.facts.molecular_factories import context_for
from tests.facts.test_molecular_contracts import quantity

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("own_assertion", [True, False])
def test_automatic_assertion_must_be_inside_own_value_not_an_ancillary_fragment(django_user_model, own_assertion):
    _, patient = _patient(django_user_model, "molecular-assertion-" + str(own_assertion))
    texts = ["12mut/Mb；明确检出" if own_assertion else "12mut/Mb", "明确检出"]
    document, version = parsed_facts(patient, texts, document_type="OTHER")
    blocks = list(version.ocr_blocks.order_by("reading_order"))
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk, title="合成原件范围",
        routing_kind="MOLECULAR", expected_lifecycle_revision=document.lifecycle_revision, expected_version_id=str(version.pk),
        spans=[{"page_number": b.document_page.page_number, "ocr_block_id": str(b.pk), "start_offset": 0, "end_offset": len(b.text)} for b in blocks])
    value = quantity("TMB", values=["12"], unit="mut/Mb", raw="12mut/Mb")
    content = field_content("assay.tmb_value", value, texts[0], entity_context=context_for(report, {"SPECIMEN": None, "ASSAY": None}),
                            source_role="CURRENT_RESULT", reported_assertion={"code": "DETECTED", "raw": "明确检出", "proof_fragment_ordinals": [0 if own_assertion else 1]})
    content["literal_source"] = {"version": "PATHOLOGY_LITERAL_SOURCE_V1", "literal_fragment_ordinals": [0], "value_fragment_ordinals": [0], "label_fragment_ordinals": []}
    raw = "\n".join(texts)
    evidence = SourceEvidence.objects.create(parsing_version=version, document_page=blocks[0].document_page,
                                              source_text=raw, confidence=.99)
    fact = Fact(document=document, document_page=blocks[0].document_page, parsing_version=version, evidence=evidence,
                origin="AUTOMATIC", category="MOLECULAR", representation="FIELD", clinical_report=report,
                field_key="assay.tmb_value", entity_key="assay:unknown", schema_version="MOLECULAR_REPORT_V1",
                raw_text=raw, automatic_content=content)
    fact.full_clean()
    fact.save()
    for ordinal, block in enumerate(blocks):
        source = SourceEvidence.objects.create(parsing_version=version, document_page=block.document_page, ocr_block=block,
                                               source_text=block.text, polygon=block.polygon, confidence=.99)
        fragment = FactSourceFragment(fact=fact, ordinal=ordinal, document_page=block.document_page, evidence=source,
            ocr_block=block, source_kind="OCR", start_offset=0, end_offset=len(block.text), raw_text=block.text, polygon=block.polygon)
        fragment.full_clean()
        fragment.save()
    if own_assertion:
        validate_context_candidate(fact)
    else:
        with pytest.raises(ValidationError, match="自己的原值窗口"):
            validate_context_candidate(fact)


@pytest.mark.parametrize("prefix", ["", "not ", "not\n", "not: "])
@pytest.mark.parametrize("split_spans", [False, True])
def test_automatic_cropped_value_proof_cannot_remove_actual_ocr_negator(django_user_model, prefix, split_spans):
    _, patient = _patient(django_user_model, "molecular-cropped-" + str(len(prefix)))
    original = "12mut/Mb；" + prefix + "negative"
    document, version = parsed_facts(patient, [original], document_type="OTHER")
    block = version.ocr_blocks.get()
    start = original.index("negative")
    spans = [{"page_number": 1, "ocr_block_id": str(block.pk), "start_offset": 0, "end_offset": len(original)}]
    if split_spans:
        spans = [{"page_number": 1, "ocr_block_id": str(block.pk), "start_offset": a, "end_offset": b} for a, b in ((0, start-1), (start, len(original)))]
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk, title="合成断言窗口",
        routing_kind="MOLECULAR", expected_lifecycle_revision=document.lifecycle_revision, expected_version_id=str(version.pk),
        spans=spans)
    content = field_content("assay.tmb_value", quantity("TMB", values=["12"], unit="mut/Mb", raw="12mut/Mb"), "negative",
        entity_context=context_for(report, {"SPECIMEN": None, "ASSAY": None}), source_role="CURRENT_RESULT",
        reported_assertion={"code": "NEGATIVE", "raw": "negative", "proof_fragment_ordinals": [0]})
    content["literal_source"] = {"version": "PATHOLOGY_LITERAL_SOURCE_V1", "literal_fragment_ordinals": [0], "value_fragment_ordinals": [0], "label_fragment_ordinals": []}
    evidence = SourceEvidence.objects.create(parsing_version=version, document_page=block.document_page, ocr_block=block,
        source_text="negative", polygon=block.polygon, confidence=.99)
    fact = Fact(document=document, document_page=block.document_page, parsing_version=version, evidence=evidence,
        origin="AUTOMATIC", category="MOLECULAR", representation="FIELD", clinical_report=report, field_key="assay.tmb_value",
        entity_key="assay:unknown", schema_version="MOLECULAR_REPORT_V1", raw_text="negative", automatic_content=content)
    fact.full_clean()
    fact.save()
    fragment = FactSourceFragment(fact=fact, ordinal=0, document_page=block.document_page, evidence=evidence, ocr_block=block,
        source_kind="OCR", start_offset=start, end_offset=len(original), raw_text="negative", polygon=block.polygon)
    fragment.full_clean()
    fragment.save()
    if not prefix:
        validate_context_candidate(fact)
    else:
        with pytest.raises(ValidationError):
            validate_context_candidate(fact)
