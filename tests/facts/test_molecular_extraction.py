"""Synthetic original OCR source maps, never labels copied from real gold."""
from copy import deepcopy

import pytest

from tests.facts.test_clinical_segments import block


def report_rows():
    return [block(text, order=i, box=(.05, .04 + i * .045, .95, .065 + i * .045)) for i, text in enumerate([
        "合成医院 分子检测报告", "报告编号：SYN-M-1", "标本编号：SYN-S1", "检测名称：SYN-NGS",
        "Panel名称：SYN-PANEL；Panel规模：约0500个基因", "采样日期：2030年4月；收样日期：未提供；报告日期：2030-04-05",
        "体细胞变异检测结果", "基因|完整表达|编码位点|蛋白位点|密码子|转录本|位置|变异丰度",
        "SYN1|c.12+1G>A (p.?)|c.12+1G>A|p.?|codon 4|NM_SYN.2|build-X chr2:12|01.20%",
        "TMB：≥08.50 mut/Mb；TMB定性：低；MSI类别：MSI-L", "说明：TNB 19；ITH 0.4；CD274拷贝数不能解释为PD-L1评分",
    ])]


def extract(rows):
    from apps.facts.molecular_segments import segment_molecular_reports
    from apps.facts.molecular_extraction import molecular_candidates
    segments, unknown = segment_molecular_reports(rows)
    return segments, unknown, [molecular_candidates(segment) for segment in segments]


def test_table_full_identity_and_msi_tmb_keep_actual_unicode_source_maps():
    rows = report_rows()
    before = [(r.text, deepcopy(r.polygon)) for r in rows]
    segments, unknown, groups = extract(rows)
    assert len(segments) == 1 and not unknown
    fields = groups[0]
    by_key = {f.key: f for f in fields}
    identity = by_key["variant.identity"].value
    assert identity["gene"] == {"state": "PRINTED", "raw": "SYN1"}
    assert identity["transcripts"] == {"state": "PRINTED", "values": ["NM_SYN.2"]}
    assert identity["coding"]["values"] == ["c.12+1G>A"]
    assert by_key["variant.allele_fraction"].value["values"] == ["01.20"]
    assert by_key["assay.tmb_value"].value["values"] == ["08.50"]
    assert by_key["assay.tmb_value"].value["comparator"] == "GE"
    assert by_key["assay.msi_category"].value["code"] == "MSI_L"
    assert not any(f.key.startswith("ihc.") for f in fields)
    for candidate in fields:
        for piece in candidate.fragments:
            assert piece.text == piece.block.text[piece.start:piece.end]
    assert [(r.text, r.polygon) for r in rows] == before


@pytest.mark.parametrize("heading", ["患者自述分子检测报告提示异常", "入院记录：上次基因检测报告", "说明：建议基因检测报告复查"])
def test_cited_title_does_not_open_primary_report(heading):
    rows = report_rows()
    rows[0].text = heading
    segments, unknown, groups = extract(rows)
    assert not segments and unknown == {1} and not groups


def test_two_reports_in_one_block_never_borrow_panel_or_variant_identity():
    first = "分子检测报告\n标本编号：SYN-A\n检测名称：SYN panel A\n体细胞变异检测结果\n基因：SYN1；完整表达：c.12+1G>A；变异丰度：01.20%\n"
    second = "基因检测报告\n标本编号：SYN-B\n检测结果：TMB：12 mut/Mb"
    segments, _, groups = extract([block(first + second)])
    assert len(segments) == 2
    assert all(p.end <= len(first) for p in segments[0].pieces)
    tmb = next(f for f in groups[1] if f.key == "assay.tmb_value")
    assert tmb.links["ASSAY"] is None
    assert not any(f.key == "variant.identity" for f in groups[1])


@pytest.mark.django_db
def test_actual_persistence_from_ocr_and_completed_idempotence(django_user_model):
    from apps.facts.clinical_extraction import extract_clinical_version
    from apps.facts.clinical_context import validate_context_candidate
    from apps.facts.clinical_readmodels import effective_field
    from tests.documents.test_detail_viewer import _patient
    from tests.facts.factories import parsed_facts
    from tests.facts.pathology_factories import review

    _, patient = _patient(django_user_model, "molecular-pipeline")
    rows = report_rows()
    document, version = parsed_facts(patient, [r.text for r in rows], document_type="OTHER")
    for source, row in zip(version.ocr_blocks.order_by("reading_order"), rows):
        source.polygon = row.polygon
        source.save(update_fields=["polygon"])
    result = extract_clinical_version(version)
    assert result.report_count == 1 and result.field_count > 8
    report = document.clinical_reports.get()
    assert report.routing_kind == "MOLECULAR"
    for fact in report.fields.order_by("reading_order"):
        fact.full_clean()
        validate_context_candidate(fact)
        for source in fact.source_fragments.all():
            source.full_clean()
            assert source.raw_text == source.ocr_block.text[source.start_offset:source.end_offset]
        review(patient, fact)
    metric = report.fields.get(field_key="variant.allele_fraction")
    assert effective_field(metric)["usable"]
    count = report.fields.count()
    assert extract_clinical_version(version).pk == result.pk and report.fields.count() == count


@pytest.mark.django_db(transaction=True)
def test_actual_upload_worker_keeps_unpublished_candidates_unusable_then_publishes(django_user_model):
    from dataclasses import replace
    from django.core.exceptions import ValidationError
    from apps.facts.clinical_context import validate_context_candidate
    from apps.facts.clinical_readmodels import effective_field
    from apps.processing.models import ParsingVersion
    from apps.processing.ocr.fake import FixtureOcrProvider
    from apps.processing.pipeline import DocumentProcessingPipeline
    from apps.processing.runner import ExecutionState, run_processing
    from apps.processing.value_objects import OcrRegion
    from tests.processing.test_pipeline import _document_and_run, _png_bytes, _ocr_page, _Store

    document, run = _document_and_run(django_user_model)
    page = replace(_ocr_page(), regions=tuple(OcrRegion(row.text, row.polygon, .98, i) for i, row in enumerate(report_rows())))
    observed = {}
    class Pipeline(DocumentProcessingPipeline):
        def _persist(self, context, document, *args, **kwargs):
            super()._persist(context, document, *args, **kwargs)
            version = ParsingVersion.objects.get(processing_run_id=context.run_id)
            metric = version.facts.get(field_key="variant.allele_fraction")
            observed["unpublished"] = not version.active and version.published_at is None
            observed["usable"] = effective_field(metric)["usable"]
            with pytest.raises(ValidationError):
                validate_context_candidate(metric)
    result = run_processing(run.pk, Pipeline(object_store=_Store(_png_bytes()), raster_provider=FixtureOcrProvider((page,))))
    assert result.state == ExecutionState.SUCCEEDED
    assert observed == {"unpublished": True, "usable": False}
    version = ParsingVersion.objects.get(processing_run=run)
    assert version.active and version.status == "PUBLISHED"
    assert version.clinical_extraction.status == "EXTRACTED"
    metric = version.facts.get(field_key="variant.allele_fraction")
    validate_context_candidate(metric)
    assert effective_field(metric)["source_valid"] and not effective_field(metric)["usable"]
