"""Typed histology consumes actual original fields, never IHC-derived diagnoses."""
from copy import deepcopy

import pytest

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.cancer_ordering.sources import SourceContext
from tests.facts.pathology_factories import review
from tests.facts.test_pathology_extraction import report_rows
from tests.facts.test_pathology_pipeline import fixture


pytestmark = pytest.mark.django_db


def typed_fixture(user_model, text="肺癌", *, name="cancer-typed", confirm_anchor=True, heading="组织学诊断"):
    rows = report_rows()
    rows[4].text = heading + "：" + text
    client, patient, document, version, extraction = fixture(user_model, rows=rows, name=name)
    field = document.facts.get(field_key="specimen.histology")
    anchor = document.facts.get(field_key="specimen.identity")
    if confirm_anchor:
        review(patient, anchor)
    return client, patient, document, version, field, anchor


@pytest.mark.parametrize("text,profile", [
    ("肺癌", "LUNG"), ("右肺上叶浸润性腺癌", "LUNG"),
    ("胰腺癌", "PANCREAS"), ("胰头癌", "PANCREAS"), ("胰头导管腺癌", "PANCREAS"),
])
def test_actual_histology_field_collects_fixed_literal_and_original_offsets(django_user_model, text, profile):
    _, patient, _, version, field, _ = typed_fixture(django_user_model, text)
    original = deepcopy(field.automatic_content)
    source = SourceContext().fact(field.pk)
    assert source.text == text
    assert source.source_valid
    runs = collect_current(patient, actor=patient.account)
    assert all(run.status == "COMPLETE" for run in runs)
    candidate = CancerCandidate.objects.get(source_fact=field)
    assert candidate.source_report_id == field.clinical_report_id
    assert candidate.original_data["label"] == text
    assert candidate.original_data["profile"] == profile
    for piece in candidate.original_source["label_fragments"]:
        actual = version.ocr_blocks.get(pk=piece["block_id"])
        assert piece["raw"] == actual.text[piece["start"]:piece["end"]]
    assert resolve_ordering(patient)["profile"] == profile
    field.refresh_from_db()
    assert field.automatic_content == original and field.revision_number == 0


def test_unreviewed_specimen_cannot_be_confirmed_by_collecting_or_candidate_review(django_user_model):
    _, patient, _, _, field, anchor = typed_fixture(django_user_model, confirm_anchor=False)
    source = SourceContext().fact(field.pk)
    assert not source.source_valid
    collect_current(patient, actor=patient.account)
    assert CancerCandidate.objects.filter(source_fact=field).exists()
    assert resolve_ordering(patient)["profile"] == "GENERAL"
    anchor.refresh_from_db()
    assert anchor.revision_number == 0


@pytest.mark.parametrize("text,assertion,subject", [
    ("未见肺癌", "NEGATED", "CURRENT_PRIMARY"),
    ("不能排除肺癌", "UNCERTAIN", "CURRENT_PRIMARY"),
    ("既往肺癌", "AFFIRMED", "HISTORICAL"),
    ("母亲患肺癌", "AFFIRMED", "OTHER_PERSON"),
    ("转移性肺癌", "AFFIRMED", "METASTATIC_SITE"),
])
def test_typed_source_flags_do_not_overwrite_literal_qualification(django_user_model, text, assertion, subject):
    _, patient, _, _, field, _ = typed_fixture(django_user_model, text)
    collect_current(patient, actor=patient.account)
    rows = list(CancerCandidate.objects.filter(source_fact=field))
    assert rows
    assert rows[0].original_data["assertion"] == assertion
    assert rows[0].original_data["subject"] == subject
    assert resolve_ordering(patient)["profile"] == "GENERAL"


@pytest.mark.parametrize("role", ["PRIMARY_ASSAY_METADATA", "SUBMITTED_HISTORY", "HISTORICAL_QUOTE", "EXPLANATION", "CONTROL", "QC", "UNKNOWN"])
def test_non_current_typed_source_cannot_become_an_auto_diagnosis(django_user_model, role):
    from apps.facts.models import Fact

    _, patient, _, _, field, _ = typed_fixture(django_user_model)
    content = deepcopy(field.automatic_content)
    content['source_role'] = role
    Fact.objects.filter(pk=field.pk).update(automatic_content=content)
    assert not SourceContext().fact(field.pk).source_valid
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


@pytest.mark.parametrize("fragment_role", ['label', 'value', 'anchor'])
@pytest.mark.parametrize("confidence", [None, '0.9499'])
def test_confidence_requires_label_value_and_actual_specimen_proof(django_user_model, fragment_role, confidence):
    from apps.processing.models import OcrBlock, SourceEvidence

    _, patient, _, _, field, anchor = typed_fixture(django_user_model)
    if fragment_role == 'anchor':
        piece = anchor.source_fragments.first()
    else:
        ordinal = field.automatic_content['literal_source'][fragment_role + '_fragment_ordinals'][0]
        piece = field.source_fragments.get(ordinal=ordinal)
    if confidence is None:
        # OCR confidence is non-null by schema; nullable original evidence
        # represents missing confidence without bypassing database constraints.
        SourceEvidence.objects.filter(pk=piece.evidence_id).update(confidence=None)
    else:
        OcrBlock.objects.filter(pk=piece.ocr_block_id).update(confidence=confidence)
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_missing_legacy_role_declaration_never_manufactures_label_proof(django_user_model):
    from apps.facts.models import Fact

    _, patient, _, _, field, _ = typed_fixture(django_user_model)
    content = deepcopy(field.automatic_content)
    del content['literal_source']
    Fact.objects.filter(pk=field.pk).update(automatic_content=content)
    source = SourceContext().fact(field.pk)
    assert not source.source_valid
    assert source.binding_kind == 'PAGE_ONLY'
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['profile'] == 'GENERAL'


def test_typed_and_excerpt_at_the_same_original_position_have_one_current_member(django_user_model):
    _, patient, _, _, field, _ = typed_fixture(django_user_model, text='肺癌。', heading='病理诊断')
    runs = collect_current(patient, actor=patient.account)
    members = [member for run in runs for member in run.members.all()]
    assert len(members) == 1, [(member.candidate.source_fact.representation,
        member.candidate.original_data['label'], member.candidate.original_source['binding_kind'],
        member.candidate.original_source['label_fragments']) for member in members]
    assert members[0].candidate.original_data['label'] == '肺癌'
    assert resolve_ordering(patient)['profile'] == 'LUNG'
