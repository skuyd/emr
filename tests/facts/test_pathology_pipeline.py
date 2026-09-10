"""Actual synthetic OCR -> report -> immutable context persistence."""
from copy import deepcopy

import pytest

from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.facts.test_pathology_extraction import report_rows


pytestmark = pytest.mark.django_db


def fixture(django_user_model, *, rows=None, name="pathology-pipeline"):
    from apps.facts.clinical_extraction import extract_clinical_version

    client, patient = _patient(django_user_model, name)
    rows = report_rows() if rows is None else rows
    document, version = parsed_facts(patient, [row.text for row in rows], document_type="PATHOLOGY")
    for source, row in zip(version.ocr_blocks.order_by("reading_order"), rows):
        source.polygon = row.polygon
        source.layout_polygon = getattr(row, "layout_polygon", None)
        source.save(update_fields=["polygon", "layout_polygon"])
    run = extract_clinical_version(version)
    return client, patient, document, version, run


def test_pipeline_persists_tps_cps_with_actual_ocr_anchor_location_proofs(django_user_model):
    from apps.facts.clinical_context import validate_context_candidate
    from apps.facts.clinical_readmodels import effective_field
    from apps.facts.models import Fact
    from apps.facts.revisions import revise_fact

    _, patient, document, version, run = fixture(django_user_model)
    assert run.report_count == 1 and run.status == "EXTRACTED"
    report = document.clinical_reports.get()
    assert report.routing_kind == "PATHOLOGY"
    fields = list(report.fields.order_by("reading_order"))
    originals = {str(field.pk): deepcopy(field.automatic_content) for field in fields}
    assert len([field for field in fields if field.field_key == "ihc.score"]) == 2
    for field in fields:
        field.full_clean()
        validate_context_candidate(field)
        for fragment in field.source_fragments.all():
            fragment.full_clean()
            assert fragment.raw_text == fragment.ocr_block.text[fragment.start_offset:fragment.end_offset]
            assert fragment.polygon == fragment.ocr_block.polygon
        for binding in field.automatic_content["entity_context"]["bindings"]:
            if binding["state"] != "BOUND":
                continue
            target = Fact.objects.get(pk=binding["target_fact_id"])
            proofs = list(field.source_fragments.filter(ordinal__in=binding["proof_fragment_ordinals"]))
            assert any(p.ocr_block_id == t.ocr_block_id and p.start_offset <= t.start_offset and p.end_offset >= t.end_offset
                       for p in proofs for t in target.source_fragments.all())
        revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0, checked_original=True,
                    expected_source=effective_field(field)["current_source_token"])
    scores = list(report.fields.filter(field_key="ihc.score"))
    assert all(effective_field(field)["usable"] for field in scores)
    assert {effective_field(field)["content"]["value"]["score_kind"] for field in scores} == {"TPS", "CPS"}
    for field in report.fields.all():
        assert field.automatic_content == originals[str(field.pk)]
    from apps.facts.clinical_extraction import extract_clinical_version
    assert extract_clinical_version(version).pk == run.pk
    assert report.fields.count() == len(fields)


def test_existing_completed_extraction_is_not_silently_backfilled(django_user_model):
    from apps.facts.models import ClinicalExtraction
    from apps.facts.clinical_extraction import extract_clinical_version

    _, patient = _patient(django_user_model, "pathology-old-run")
    document, version = parsed_facts(patient, [row.text for row in report_rows()], document_type="PATHOLOGY")
    previous = ClinicalExtraction.objects.create(parsing_version=version, extractor_version="clinical-imaging-v2",
                                                 schema_version="1.1", status="NO_REPORTS", report_count=0,
                                                 field_count=0, unparsed_page_count=1)
    assert extract_clinical_version(version).pk == previous.pk
    assert not document.clinical_reports.exists()
