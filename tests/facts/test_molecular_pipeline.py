"""Actual source-located ORM integration using synthetic report originals."""
from copy import deepcopy

import pytest

from tests.facts.test_molecular_extraction import report_rows
from tests.facts.test_clinical_segments import block

pytestmark = pytest.mark.django_db


def fixture(django_user_model, rows, name="molecular-orm"):
    from tests.documents.test_detail_viewer import _patient
    from tests.facts.factories import parsed_facts
    from apps.facts.clinical_extraction import extract_clinical_version
    _, patient = _patient(django_user_model, name)
    document, version = parsed_facts(patient, [r.text for r in rows], document_type="OTHER")
    for actual, row in zip(version.ocr_blocks.order_by("reading_order"), rows):
        actual.polygon = row.polygon
        actual.save(update_fields=["polygon"])
    return patient, document, version, extract_clinical_version(version)


def confirm_report(patient, report):
    from apps.facts.clinical_context import validate_context_candidate
    from apps.facts.clinical_schema import FIELDS
    from tests.facts.pathology_factories import review
    for fact in sorted(report.fields.all(), key=lambda f: FIELDS[f.field_key].rank):
        validate_context_candidate(fact)
        for fragment in fact.source_fragments.all():
            fragment.full_clean()
            assert fragment.raw_text == fragment.ocr_block.text[fragment.start_offset:fragment.end_offset]
        review(patient, fact)


def test_mixed_pdl1_shared_ihc_context_is_current_only_under_its_actual_assay(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    from apps.facts.pathology_schema import CONTEXT
    rows = report_rows()[:-1] + [block("检测名称：PD-L1免疫组化\n检测结果：PD-L1 TPS：13% CPS：21", order=20)]
    patient, document, _, _ = fixture(django_user_model, rows)
    report = document.clinical_reports.get()
    confirm_report(patient, report)
    scores = list(report.fields.filter(field_key="ihc.score"))
    assert len(scores) == 2
    assert all(effective_field(f)["usable"] for f in scores)
    for score in scores:
        context = score.automatic_content["entity_context"]
        assert context["context_version"] == CONTEXT
        assert set(context) == {"context_version", "report_id", "membership_policy", "bindings"}
        binding = next(b for b in context["bindings"] if b["role"] == "ASSAY")
        assert report.fields.get(pk=binding["target_fact_id"]).automatic_content["value"]["raw"] == "PD-L1免疫组化"


def test_scoped_negative_and_ordered_drug_set_persist_and_qualify_with_exact_source(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    rows = report_rows()[:-1]
    rows += [block("基因|完整表达|编码位点|蛋白位点|密码子|转录本|位置|变异丰度\n"
                   "SYN2|c.3G>C|c.3G>C|p.?|codon 1|NM_SYN2.3|build-X chr3:3|2%\n"
                   "药物：SYN-A 与 SYN-B；关联变异：c.12+1G>A (p.?) 及 c.3G>C；依据方向：耐药；证据等级：II；等级体系：SYN-GRADE-v2\n"
                   "检测范围结论：本范围未检出拷贝数改变；检测范围：SYN panel拷贝数范围；检测种类：拷贝数", order=20)]
    patient, document, _, _ = fixture(django_user_model, rows)
    report = document.clinical_reports.get()
    confirm_report(patient, report)
    for key in ("assay.negative_statement", "drug_evidence.drugs", "drug_evidence.level"):
        fact = report.fields.get(field_key=key)
        assert effective_field(fact)["usable"], (key, effective_field(fact))
    drug = report.fields.get(field_key="drug_evidence.drugs")
    targets = [b["target_fact_id"] for b in drug.automatic_content["entity_context"]["bindings"] if b["role"] == "VARIANT"]
    assert len(targets) == 2 and len(set(targets)) == 2
    assert [report.fields.get(pk=t).automatic_content["value"]["gene"]["raw"] for t in targets] == ["SYN1", "SYN2"]


def test_incomplete_identity_stays_visible_but_cannot_qualify_numeric_result(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    rows = [block("分子检测报告\n标本编号：SYN-S\n检测名称：SYN-NGS\n体细胞变异检测结果\n基因：SYN1；完整表达：c.1A>T；变异丰度：1%")]
    patient, document, _, result = fixture(django_user_model, rows)
    report = document.clinical_reports.get()
    confirm_report(patient, report)
    metric = report.fields.get(field_key="variant.allele_fraction")
    assert effective_field(metric)["source_valid"] and not effective_field(metric)["usable"]
    assert result.status == "PARTIAL" and "incomplete_molecular_identity" in result.limitations


def test_persistence_failure_rolls_back_all_reports_fragments_and_facts(django_user_model, monkeypatch):
    from django.core.exceptions import ValidationError
    from apps.facts import molecular_persistence
    from apps.facts.clinical_extraction import extract_clinical_version
    from apps.facts.models import ClinicalExtraction, ClinicalReport, FactSourceFragment
    from apps.processing.models import SourceEvidence
    from tests.documents.test_detail_viewer import _patient
    from tests.facts.factories import parsed_facts
    _, patient = _patient(django_user_model, "molecular-atomic")
    document, version = parsed_facts(patient, [r.text for r in report_rows()], document_type="OTHER")
    originals = deepcopy(list(version.facts.values()))
    evidence_count = SourceEvidence.objects.count()
    original = molecular_persistence.validate_context_candidate
    def reject_metric(fact, **kwargs):
        if fact.field_key == "variant.allele_fraction":
            raise ValidationError("synthetic late molecular source failure")
        return original(fact, **kwargs)
    monkeypatch.setattr(molecular_persistence, "validate_context_candidate", reject_metric)
    with pytest.raises(ValidationError, match="synthetic late"):
        extract_clinical_version(version)
    assert not ClinicalReport.objects.filter(document=document).exists()
    assert not ClinicalExtraction.objects.filter(parsing_version=version).exists()
    assert not FactSourceFragment.objects.filter(fact__document=document).exists()
    assert list(version.facts.values()) == originals
    assert SourceEvidence.objects.count() == evidence_count
    monkeypatch.setattr(molecular_persistence, "validate_context_candidate", original)
    assert extract_clinical_version(version).field_count > 8
