from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


CT = [
    "合成医院 CT诊断报告书",
    "检查日期：2026-08-17 09:20 检查项目：胸部CT平扫",
    "影像表现：左肺上叶见结节，大小约12mm×9mm×8mm。右肺下叶见结节，直径约6mm。",
    "诊断意见：双肺结节，建议结合临床。",
    "报告日期：2026-08-18 报告医师：合成医师",
]


def clinical_fixture(django_user_model, *, texts=None, name="clinical", document=None, previous=None):
    from apps.facts.clinical_extraction import extract_clinical_version

    client, patient = _patient(django_user_model, name) if document is None else (None, document.patient)
    document, version = parsed_facts(patient, texts or CT, document=document, previous=previous, document_type="IMAGING")
    run = extract_clinical_version(version)
    return client, patient, document, version, run


def test_ocr_findings_persist_seven_typed_fields_and_distinct_lesion_sources(django_user_model):
    from apps.facts.models import ClinicalReport, Fact

    _, _, document, version, run = clinical_fixture(django_user_model)
    report = ClinicalReport.objects.get(document=document)
    fields = list(Fact.objects.filter(clinical_report=report, representation="FIELD"))
    assert run.status == "EXTRACTED"
    assert {f.field_key for f in fields} == {
        "report.exam_date", "imaging.modality", "imaging.body_site", "lesion.site",
        "lesion.laterality", "lesion.dimensions", "imaging.impression",
    }
    measured = [f for f in fields if f.field_key == "lesion.dimensions"]
    assert [[c["value"] for c in f.automatic_content["value"]["components"]] for f in measured] == [["12", "9", "8"], ["6"]]
    assert len({f.entity_key for f in measured}) == 2
    assert next(f for f in fields if f.field_key == "report.exam_date").automatic_content["value"] == {
        "value": "2026-08-17", "precision": "DAY",
    }
    for field in fields:
        field.full_clean()
        assert field.evidence.source_text == field.raw_text
        assert field.source_fragments.exists()
        for fragment in field.source_fragments.all():
            assert fragment.raw_text == fragment.ocr_block.text[fragment.start_offset:fragment.end_offset]
            assert fragment.polygon == fragment.ocr_block.polygon
            fragment.full_clean()
    assert report.spans.filter(document_page__document=document).exists()
    from apps.facts.clinical_extraction import extract_clinical_version
    assert extract_clinical_version(version).pk == run.pk
    assert Fact.objects.filter(clinical_report=report).count() == len(fields)


def test_unassigned_edge_ocr_stays_available_with_persisted_boundary_and_review_notice(django_user_model):
    from apps.facts.clinical_extraction import extract_clinical_version
    from tests.facts.test_clinical_segments import adjacent_page_rows

    rows = adjacent_page_rows()
    client, patient = _patient(django_user_model, "clinical-edge")
    document, version = parsed_facts(patient, [row.text for row in rows], document_type="IMAGING")
    blocks = list(version.ocr_blocks.order_by("reading_order"))
    for source, row in zip(blocks, rows):
        source.polygon = row.polygon
        source.save(update_fields=["polygon"])
    edge = blocks[4]
    original = (edge.text, edge.polygon)
    run = extract_clinical_version(version)
    report = document.clinical_reports.get()
    assert run.status == "PARTIAL" and report.boundary_state == "LIMITED"
    assert report.limitations == ["unassigned_page_edge_text"]
    assert not report.spans.filter(ocr_block=edge).exists()
    fields = list(report.fields.filter(field_key__startswith="lesion."))
    assert len(fields) == 3
    for field in fields:
        assert field.raw_text == rows[3].text[:-1]
        assert not field.source_fragments.filter(ocr_block=edge).exists()
        assert "unassigned_page_edge_text" in field.automatic_content["limitations"]
        assert "页面边缘有尚未归属本报告的文字" in client.get(f"/facts/{field.pk}/").content.decode()
    assert "页面边缘有尚未归属本报告的文字" in client.get(f"/facts/reports/{report.pk}/").content.decode()
    edge.refresh_from_db()
    assert (edge.text, edge.polygon) == original


def test_single_block_report_boundaries_keep_raw_unicode_offsets(django_user_model):
    from apps.facts.models import ClinicalReport, Fact

    first = "CT诊断报告书\n检查日期：2026-08-01\n检查项目：胸部CT\n影像表现：左肺见Ⅲ类结节，大小12×8mm。\n诊断意见：左肺结节。\n"
    second = "MR诊断报告书\n检查日期：2026-08-02\n检查项目：颅脑磁共振\n影像表现：右额叶见结节，大小4×3mm。\n诊断意见：右额叶结节。"
    _, _, document, _, _ = clinical_fixture(django_user_model, texts=[first + second], name="raw-offset")
    reports = list(ClinicalReport.objects.filter(document=document).order_by("ordinal"))
    assert len(reports) == 2
    assert reports[1].spans.first().start_offset == len(first)
    assert reports[0].spans.last().end_offset == len(first)
    assert "MR诊断报告书" not in "".join(s.raw_text for s in reports[0].spans.all())
    dates = [Fact.objects.get(clinical_report=r, field_key="report.exam_date").automatic_content["value"]["value"] for r in reports]
    assert dates == ["2026-08-01", "2026-08-02"]


def test_field_schema_rejects_boolean_nan_unknown_keys_and_invented_axis():
    from apps.facts.clinical_schema import field_content, validate_content

    value = {"components": [{"value": "12", "unit": "mm", "axis": None}],
             "approximate": True, "measurement_role": "CURRENT", "raw": "约12mm"}
    content = field_content("lesion.dimensions", value, "约12mm")
    validate_content(content)
    for bad in (True, "NaN", "-2", "Infinity"):
        invalid = deepcopy(content)
        invalid["value"]["components"][0]["value"] = bad
        with pytest.raises(ValidationError):
            validate_content(invalid)
    invalid = deepcopy(content)
    invalid["value"]["components"][0]["axis"] = "RECIST_TARGET"
    with pytest.raises(ValidationError):
        validate_content(invalid)
    with pytest.raises(ValidationError):
        field_content("diagnosis.automatic_diagnosis", {"text": "invented"}, "invented")


def test_title_in_history_and_comparison_date_do_not_create_or_re_date_report(django_user_model):
    from apps.facts.models import ClinicalReport, Fact

    texts = ["入院记录：患者说上次CT诊断报告书提到肺结节。", *CT]
    texts[3] = "影像表现：对比前片（2025-01-01）：左肺见结节，大小约12×8mm。"
    _, _, document, _, _ = clinical_fixture(django_user_model, texts=texts, name="history-title")
    report = ClinicalReport.objects.get(document=document)
    assert Fact.objects.get(clinical_report=report, field_key="report.exam_date").automatic_content["value"]["value"] == "2026-08-17"
    assert "患者说" not in "".join(s.raw_text for s in report.spans.all())


def test_typed_actions_preserve_original_and_reparse_requires_fresh_confirmation(django_user_model):
    from apps.facts.clinical_readmodels import review_reports
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact

    _, patient, document, version, _ = clinical_fixture(django_user_model, name="field-review")
    fact = Fact.objects.get(parsing_version=version, field_key="imaging.impression")
    original = deepcopy(fact.automatic_content)
    for action, expected in [("DEFER", "DEFERRED"), ("CONFIRM", "CONFIRMED"), ("EXCLUDE", "EXCLUDED"),
                             ("UNDO", "CONFIRMED"), ("REVOKE", "PENDING"), ("CORRECT", "CONFIRMED")]:
        row = effective_fact(fact)
        revise_fact(patient, fact.pk, actor=patient.account, action=action, expected_revision=fact.revision_number,
                    expected_source=row["current_source_token"], checked_original=True,
                    changes={"value": {"text": "双肺结节，请随诊。"}, "raw_value": "双肺结节，请随诊。"} if action == "CORRECT" else None)
        fact.refresh_from_db()
        assert effective_fact(fact)["status"] == expected
    assert fact.automatic_content == original
    assert fact.revisions.last().before["content"] == original
    _, _, _, second, _ = clinical_fixture(django_user_model, document=document, previous=version)
    current = review_reports(patient, actor=patient.account)
    assert all(not row["usable"] for report in current for row in report["fields"])
    assert second.pk != version.pk
    assert not effective_fact(fact)["source_valid"]


def test_manual_report_requires_page_and_actor_and_first_ocr_invalidates_confirmation(django_user_model):
    from apps.facts.clinical_services import create_manual_report, add_manual_clinical_field
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact
    from tests.documents.test_detail_viewer import _document

    _, patient = _patient(django_user_model, "manual-field")
    document, _ = _document(patient, status="PROCESSING_FAILED")
    with pytest.raises(ValidationError):
        create_manual_report(patient, actor=patient.account, document_id=document.pk, spans=[], title="人工核对报告", expected_lifecycle_revision=document.lifecycle_revision, expected_version_id=None)
    report = create_manual_report(patient, actor=patient.account, document_id=document.pk, spans=[{"page_number": 1}],
                                  title="人工核对报告", expected_lifecycle_revision=document.lifecycle_revision, expected_version_id=None)
    fact = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk, entity_key="report",
                                    field_key="report.exam_date", value={"value": "2026-08", "precision": "MONTH"},
                                    fragments=[{"page_number": 1, "raw_text": "检查日期：2026年8月"}],
                                    expected_report_source=report_source_token(report))
    assert fact.created_by_id == patient.account_id and report.created_by_id == patient.account_id
    revise_fact(patient, fact.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(fact)["current_source_token"], checked_original=True)
    assert effective_fact(fact)["usable"]
    clinical_fixture(django_user_model, document=document)
    fact.refresh_from_db()
    assert not effective_fact(fact)["usable"]


def test_report_exclusion_audits_every_field_and_undo_detects_intervening_change(django_user_model):
    from apps.facts.clinical_services import revise_report
    from apps.facts.clinical_readmodels import report_source_token
    from apps.facts.models import ClinicalReport
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import FactConflict, revise_fact

    _, patient, document, _, _ = clinical_fixture(django_user_model, name="report-exclude")
    report = ClinicalReport.objects.get(document=document)
    fields = list(report.fields.all())
    for fact in fields:
        revise_fact(patient, fact.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                    expected_source=effective_fact(fact)["current_source_token"], checked_original=True)
    revision = revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE", expected_revision=0,
                             expected_source=report_source_token(report))
    assert len(revision.field_revisions) == len(fields)
    assert all(not effective_fact(f)["usable"] for f in report.fields.all())
    report.refresh_from_db()
    revise_report(patient, actor=patient.account, report_id=report.pk, action="UNDO", expected_revision=1,
                  expected_source=report_source_token(report))
    for field in report.fields.all():
        row = effective_fact(field)
        if row.get("laterality_scope", {}).get("binding_id"):
            assert row["status"] == "PENDING" and not row["usable"]
            assert field.revisions.order_by("-sequence").first().action == "REVOKE"
        else:
            assert row["status"] == "CONFIRMED" and row["usable"]
    report.refresh_from_db()
    revise_report(patient, actor=patient.account, report_id=report.pk, action="EXCLUDE", expected_revision=2,
                  expected_source=report_source_token(report))
    fact = report.fields.first()
    revise_fact(patient, fact.pk, actor=patient.account, action="DEFER", expected_revision=fact.revision_number,
                expected_source=effective_fact(fact)["current_source_token"])
    report.refresh_from_db()
    with pytest.raises(FactConflict):
        revise_report(patient, actor=patient.account, report_id=report.pk, action="UNDO", expected_revision=3,
                      expected_source=report_source_token(report))
    assert report.revisions.count() == 3


def test_returning_to_old_version_cannot_resurrect_confirmation_through_undo(django_user_model):
    from apps.facts.models import Fact
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import FactConflict, revise_fact
    from apps.processing.models import ParsingVersion

    _, patient, document, version, _ = clinical_fixture(django_user_model, name="activation-field-token")
    fact = Fact.objects.get(parsing_version=version, field_key="imaging.impression")
    revise_fact(patient, fact.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(fact)["current_source_token"], checked_original=True)
    fact.refresh_from_db()
    revise_fact(patient, fact.pk, actor=patient.account, action="EXCLUDE", expected_revision=1,
                expected_source=effective_fact(fact)["current_source_token"])
    _, _, _, second, _ = clinical_fixture(django_user_model, document=document, previous=version)
    ParsingVersion.objects.activate(version.pk)
    fact.refresh_from_db()
    assert effective_fact(fact)["status"] == "PENDING"
    with pytest.raises(FactConflict):
        revise_fact(patient, fact.pk, actor=patient.account, action="UNDO", expected_revision=2,
                    expected_source=effective_fact(fact)["current_source_token"])
    assert not effective_fact(fact)["usable"]


def test_missing_ocr_page_remains_counted_and_source_fragment_cannot_forge_another_box(django_user_model):
    from apps.documents.models import DocumentPage
    from apps.facts.clinical_extraction import extract_clinical_version
    from apps.facts.models import Fact

    _, patient = _patient(django_user_model, "clinical-source-integrity")
    document, version = parsed_facts(patient, CT, document_type="IMAGING")
    DocumentPage.objects.create(document=document, page_number=2, width=800, height=1000)
    run = extract_clinical_version(version)
    assert run.status == "PARTIAL" and run.unparsed_page_count == 1
    fact = Fact.objects.get(parsing_version=version, field_key="imaging.impression")
    fragment = fact.source_fragments.get()
    fragment.evidence = Fact.objects.get(parsing_version=version, field_key="report.exam_date").evidence
    with pytest.raises(ValidationError):
        fragment.full_clean()


def test_delete_restore_fences_confirmation_and_permanent_purge_removes_all_clinical_children(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document, permanently_delete_from_trash
    from apps.documents.deletion import purge_document_deletion
    from apps.facts.models import ClinicalReport, ClinicalReportSpan, Fact, FactSourceFragment
    from apps.facts.readmodels import effective_fact
    from apps.facts.revisions import revise_fact
    from tests.documents.fakes import InMemoryObjectStore

    _, patient, document, version, _ = clinical_fixture(django_user_model, name="clinical-delete")
    field = Fact.objects.get(parsing_version=version, field_key="imaging.impression")
    revise_fact(patient, field.pk, actor=patient.account, action="CONFIRM", expected_revision=0,
                expected_source=effective_fact(field)["current_source_token"], checked_original=True)
    report_id = field.clinical_report_id
    move_to_trash(patient, document.pk, actor=patient.account)
    restore_document(patient, document.pk, actor=patient.account)
    field = Fact.objects.get(pk=field.pk)
    assert effective_fact(field)["status"] == "PENDING" and not effective_fact(field)["usable"]
    move_to_trash(patient, document.pk, actor=patient.account)
    permanently_delete_from_trash(patient, document.pk, actor=patient.account, dispatch=lambda _: None)
    document.refresh_from_db()
    assert purge_document_deletion(document.deletion_job.pk, InMemoryObjectStore()).outcome == "PURGED"
    assert not ClinicalReport.objects.filter(pk=report_id).exists()
    assert not ClinicalReportSpan.objects.exists() and not FactSourceFragment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_actual_processing_pipeline_publishes_report_fields_with_original_geometry(django_user_model, monkeypatch):
    from dataclasses import replace
    import io
    from apps.processing.images import prepare_image
    from apps.processing.models import ParsingVersion
    from apps.processing.ocr.fake import FixtureOcrProvider
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.processing.runner import ExecutionState, run_processing
    from apps.processing.value_objects import OcrRegion
    from tests.processing.test_pipeline import _Store, _document_and_run, _ocr_page, _png_bytes

    document, run = _document_and_run(django_user_model)
    payload = _png_bytes()
    prepared = prepare_image(io.BytesIO(payload), "image/png")
    prepared.pages = (replace(prepared.pages[0], source_transform=((.6, 0., .2), (0., .6, .1), (0., 0., 1.))),)
    monkeypatch.setattr("apps.processing.pipeline.prepare_document", lambda *_: prepared)
    page = replace(_ocr_page(), regions=tuple(OcrRegion(text, ((.1, .1 + i*.1), (.8, .1 + i*.1),
                                                              (.8, .15 + i*.1), (.1, .15 + i*.1)), .98, i)
                                             for i, text in enumerate(CT)))
    pipeline = DocumentProcessingPipeline(object_store=_Store(payload), raster_provider=FixtureOcrProvider((page,)))
    assert run_processing(run.pk, pipeline).state == ExecutionState.SUCCEEDED
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.clinical_extraction.status == "EXTRACTED"
    dimensions = list(version.facts.filter(field_key="lesion.dimensions"))
    assert len(dimensions) == 2
    assert dimensions[0].source_fragments.first().polygon[0] == pytest.approx([.26, .28])
