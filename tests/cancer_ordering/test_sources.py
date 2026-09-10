from copy import deepcopy
import hashlib

import pytest

from apps.cancer_ordering.matching import literal_candidates
from apps.cancer_ordering.sources import SourceContext
from apps.documents.models import Document
from apps.facts.models import Fact
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import add_manual_fact, revise_fact
from apps.processing.models import OcrBlock, ParsingVersion
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _document, _patient
from tests.facts.factories import parsed_facts


pytestmark = pytest.mark.django_db


def _source(patient, texts):
    document, version = parsed_facts(patient, texts)
    fact = Fact.objects.filter(parsing_version=version, representation='EXCERPT').first()
    assert fact is not None
    return document, version, fact


def _verify_fragments(fragments, version):
    assert fragments
    for item in fragments:
        block = OcrBlock.objects.get(pk=item['block_id'], parsing_version=version)
        assert item['page_id'] == str(block.document_page_id)
        assert item['reading_order'] == block.reading_order
        assert item['raw'] == block.text[item['start']:item['end']]
        assert item['polygon'] == block.polygon


def test_source_binds_explicit_heading_and_candidate_to_actual_original_blocks(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-blocks')
    _, version, fact = _source(patient, ['出院诊断：', '肺癌。'])
    source = SourceContext().fact(fact.pk)
    assert source.source_valid and source.binding_kind == 'OCR'
    _verify_fragments(source.fragments, version)
    assert len({item['block_id'] for item in source.fragments}) == 2
    row, = literal_candidates(source.text, source.category)
    bound = source.candidate_binding(row)
    _verify_fragments(bound['fragments'], version)
    _verify_fragments(bound['label_fragments'], version)
    assert ''.join(item['raw'] for item in bound['label_fragments']) == '肺癌'


def test_wrapped_literal_offsets_stay_in_each_original_unicode_block(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-wrap')
    _, version, fact = _source(patient, ['病理诊断：右肺上叶浸润性', '腺癌 pT2aN1M0 ⅢA期。'])
    source = SourceContext().fact(fact.pk)
    assert source.binding_kind == 'OCR'
    row, = literal_candidates(source.text, source.category)
    fragments = source.candidate_binding(row)['label_fragments']
    _verify_fragments(fragments, version)
    assert len(fragments) == 2 and ''.join(item['raw'] for item in fragments) == '右肺上叶浸润性腺癌'


def test_repeated_diagnosis_occurrences_have_distinct_original_offsets(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-repeat')
    _, version, fact = _source(patient, ['出院诊断：肺癌；肺癌。'])
    source = SourceContext().fact(fact.pk)
    rows = literal_candidates(source.text, source.category)
    spans = [source.candidate_binding(row)['label_fragments'] for row in rows]
    assert len(spans) == 2 and all(spans)
    assert spans[0] != spans[1]
    for fragments in spans:
        _verify_fragments(fragments, version)


def test_parent_confirmation_changes_identity_even_if_old_excerpt_token_is_identical(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-revision')
    _, _, fact = _source(patient, ['出院诊断：肺癌。'])
    original_token = effective_fact(fact)['current_source_token']
    before = SourceContext().fact(fact.pk)
    revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=0, checked_original=True)
    fact.refresh_from_db()
    after = SourceContext().fact(fact.pk)
    assert effective_fact(fact)['current_source_token'] == original_token
    assert before.input_fingerprint != after.input_fingerprint and before.source_token != after.source_token
    assert after.status == 'CONFIRMED'


def test_historical_author_purge_changes_source_identity_under_a_later_active_author(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-authors')
    actor = django_user_model.objects.create(phone_hash=hashlib.sha256(b'cancer-source-author').hexdigest(), phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=actor, role='EDITOR')
    _, _, fact = _source(patient, ['出院诊断：肺癌。'])
    revise_fact(patient, fact.pk, actor=actor, action='DEFER', expected_revision=0)
    revise_fact(patient, fact.pk, actor=patient.account, action='CONFIRM', expected_revision=1, checked_original=True)
    before = SourceContext().fact(fact.pk)
    actor.delete()
    after = SourceContext().fact(fact.pk)
    assert before.input_fingerprint != after.input_fingerprint
    assert before.source_token != after.source_token
    assert fact.revisions.order_by('sequence').first().author_id is None


def test_source_correction_preserves_original_and_uses_explicit_transcription_binding(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-correction')
    _, _, fact = _source(patient, ['出院诊断：肺癌。'])
    original = deepcopy(fact.automatic_content)
    before = SourceContext().fact(fact.pk)
    revise_fact(patient, fact.pk, actor=patient.account, action='CORRECT', expected_revision=0,
                checked_original=True, changes={'text': '出院诊断：胰腺癌。'})
    after = SourceContext().fact(fact.pk)
    assert after.text == '出院诊断：胰腺癌。' and after.binding_kind == 'TRANSCRIBED'
    assert after.confidence_values == () and after.source_valid
    assert before.input_fingerprint != after.input_fingerprint
    fact.refresh_from_db()
    assert fact.raw_text == '出院诊断：肺癌。' and fact.automatic_content == original


def test_manual_excerpt_without_a_parsing_version_keeps_page_proof_without_fake_ocr(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-manual')
    document, _ = _document(patient)
    fact = add_manual_fact(patient, document.pk, actor=patient.account, page_number=1,
                           category='DIAGNOSIS', text='临床诊断：肺癌。')
    source = SourceContext().fact(fact.pk)
    assert source.source_valid and source.binding_kind == 'TRANSCRIBED'
    assert not source.confidence_values
    assert source.fragments and all(item['block_id'] is None and item['start'] is None for item in source.fragments)


def test_ambiguous_identical_sections_inside_one_provider_block_do_not_get_guessed_offsets(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-ambiguity')
    _, version, _ = _source(patient, ['出院诊断：肺癌。\n出院诊断：肺癌。'])
    facts = list(Fact.objects.filter(parsing_version=version, representation='EXCERPT'))
    assert len(facts) == 2
    for fact in facts:
        source = SourceContext().fact(fact.pk)
        assert source.binding_kind == 'PAGE_ONLY' and not source.confidence_values
        assert all(item['block_id'] is None for item in source.fragments)


def test_normal_ready_publication_changes_live_token_but_preserves_collection_input(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-publish')
    _, version, fact = _source(patient, ['出院诊断：肺癌。'])
    ParsingVersion.objects.filter(pk=version.pk).update(active=False, status='READY', published_at=None)
    before = SourceContext().fact(fact.pk)
    ParsingVersion.objects.activate(version.pk)
    after = SourceContext().fact(fact.pk)
    assert not before.source_valid and after.source_valid
    assert before.input_fingerprint == after.input_fingerprint
    assert before.source_token != after.source_token


def test_zero_excerpt_diagnosis_scope_is_explicit_and_new_manual_input_is_not_hidden(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-empty')
    document, version = parsed_facts(patient, ['仅为合成说明，无明确诊断栏目。'])
    scopes = SourceContext().scopes(patient)
    assert len(scopes) == 1 and scopes[0].parsing_version.pk == version.pk
    assert scopes[0].complete and not scopes[0].facts
    add_manual_fact(patient, document.pk, actor=patient.account, page_number=1,
                    category='DIAGNOSIS', text='临床诊断：胰腺癌。')
    newer = SourceContext().scopes(patient)
    assert len(newer) == 2 and sum(len(scope.facts) for scope in newer) == 1


def test_document_lifecycle_does_not_reuse_a_previously_observed_source_token(django_user_model):
    from django.utils import timezone
    _, patient = _patient(django_user_model, 'cancer-source-lifecycle')
    document, _, fact = _source(patient, ['出院诊断：肺癌。'])
    before = SourceContext().fact(fact.pk)
    Document.objects.filter(pk=document.pk).update(deleted_at=timezone.now(), lifecycle_revision=1)
    unavailable = SourceContext().fact(fact.pk)
    Document.objects.filter(pk=document.pk).update(deleted_at=None, lifecycle_revision=2)
    restored = SourceContext().fact(fact.pk)
    assert not unavailable.source_valid and restored.source_valid
    assert len({before.source_token, unavailable.source_token, restored.source_token}) == 3


def test_parent_exclusion_is_source_unavailable_even_with_unchanged_original_ocr(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-source-excluded')
    _, _, fact = _source(patient, ['出院诊断：肺癌。'])
    before = SourceContext().fact(fact.pk)
    revise_fact(patient, fact.pk, actor=patient.account, action='EXCLUDE', expected_revision=0)
    after = SourceContext().fact(fact.pk)
    assert before.source_valid and not after.source_valid and after.status == 'EXCLUDED'
    assert before.input_fingerprint != after.input_fingerprint
