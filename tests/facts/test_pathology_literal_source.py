"""Own literal/value/label ranges and target proofs use actual OCR positions."""
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_context import validate_context_candidate
from apps.facts.clinical_schema import validate_content
from apps.facts.models import Fact, FactSourceFragment
from tests.facts.test_pathology_named_report_routing import named_report_rows
from tests.facts.test_pathology_pipeline import fixture


pytestmark = pytest.mark.django_db
VERSION = 'PATHOLOGY_LITERAL_SOURCE_V1'


def declaration(literal=(0,), value=(0,), label=()):
    return {'version': VERSION, 'literal_fragment_ordinals': list(literal),
            'value_fragment_ordinals': list(value), 'label_fragment_ordinals': list(label)}


def declare(field, data):
    content = deepcopy(field.automatic_content)
    content['literal_source'] = data
    Fact.objects.filter(pk=field.pk).update(automatic_content=content)
    return Fact.objects.get(pk=field.pk)


def proof_text(field, role):
    pieces = {p.ordinal: p for p in field.source_fragments.all()}
    return '\n'.join(pieces[i].raw_text for i in field.automatic_content['literal_source'][role + '_fragment_ordinals'])


def test_new_table_fields_persist_distinct_own_value_label_and_full_marker_window(django_user_model):
    _, _, document, _, _ = fixture(django_user_model, rows=named_report_rows(title='免疫组化检测报告单'))
    fields = list(document.facts.filter(representation='FIELD', category='PATHOLOGY'))
    assert fields and all(isinstance(f.automatic_content.get('literal_source'), dict) for f in fields)
    marker = next(f for f in fields if f.field_key == 'ihc.marker')
    assert marker.automatic_content['value'] == {'code': 'PD_L1', 'label': 'PD-L1', 'raw': 'PD-L1'}
    assert proof_text(marker, 'literal') == 'PD-L1'
    assert proof_text(marker, 'value') == 'PD-L1蛋白表达水平'
    assert proof_text(marker, 'label') == '检测项目'
    expected = {'assay.method': ('IHC', '检测方法'), 'assay.antibody': ('SYN-CLONE', '检测抗体')}
    for field in fields:
        validate_context_candidate(field)
        assert proof_text(field, 'literal') == field.automatic_content['raw_value']
        if field.field_key in expected:
            assert (proof_text(field, 'value'), proof_text(field, 'label')) == expected[field.field_key]
        for piece in field.source_fragments.all():
            assert piece.raw_text == piece.ocr_block.text[piece.start_offset:piece.end_offset]
            assert piece.polygon == piece.ocr_block.polygon
    for score in (f for f in fields if f.field_key == 'ihc.score'):
        assert '检测结果' in proof_text(score, 'label')
        marker_binding = next(b for b in score.automatic_content['entity_context']['bindings'] if b['role'] == 'MARKER')
        by_ordinal = {p.ordinal: p for p in score.source_fragments.all()}
        assert any(p.raw_text == 'PD-L1蛋白表达水平' for p in (by_ordinal[i] for i in marker_binding['proof_fragment_ordinals']))


@pytest.mark.parametrize('problem', ['null', 'version', 'boolean', 'duplicate', 'missing', 'extra'])
def test_declared_role_shape_is_checked_without_changing_legacy_schema(django_user_model, problem):
    _, _, document, _, _ = fixture(django_user_model, name='literal-shape-' + problem)
    field = document.facts.filter(field_key='ihc.score').first()
    data = declaration()
    if problem == 'null':
        data = None
    elif problem == 'version':
        data['version'] = 'UNKNOWN'
    elif problem == 'boolean':
        data['value_fragment_ordinals'] = [True]
    elif problem == 'duplicate':
        data['literal_fragment_ordinals'] = [0, 0]
    elif problem == 'missing':
        del data['label_fragment_ordinals']
    else:
        data['predicted_label'] = 'not evidence'
    content = deepcopy(field.automatic_content)
    content['literal_source'] = data
    with pytest.raises(ValidationError):
        validate_content(content)


@pytest.mark.parametrize('problem', ['missing_ordinal', 'ancestor_instead_of_value'])
def test_declared_value_must_cover_its_own_literal_positions(django_user_model, problem):
    _, _, document, _, _ = fixture(django_user_model, name='literal-own-' + problem)
    score = document.facts.filter(field_key='ihc.score').first()
    binding = next(b for b in score.automatic_content['entity_context']['bindings'] if b['role'] == 'SPECIMEN')
    value = [999] if problem == 'missing_ordinal' else binding['proof_fragment_ordinals']
    score = declare(score, declaration(value=value))
    with pytest.raises(ValidationError):
        validate_context_candidate(score)


def test_new_automatic_binding_cannot_borrow_target_ancestor(django_user_model):
    _, _, document, _, _ = fixture(django_user_model, name='literal-binding-ancestor')
    score = document.facts.filter(field_key='ihc.score').first()
    marker = document.facts.get(field_key='ihc.marker')
    declare(marker, declaration())
    score = declare(score, declaration())
    content = deepcopy(score.automatic_content)
    bindings = {b['role']: b for b in content['entity_context']['bindings']}
    bindings['MARKER']['proof_fragment_ordinals'] = list(bindings['SPECIMEN']['proof_fragment_ordinals'])
    Fact.objects.filter(pk=score.pk).update(automatic_content=content)
    with pytest.raises(ValidationError):
        validate_context_candidate(Fact.objects.get(pk=score.pk))


def test_same_literal_elsewhere_is_not_position_coverage(django_user_model):
    from apps.processing.models import SourceEvidence
    from tests.facts.test_pathology_extraction import report_rows
    rows = report_rows()
    rows[4].text = '检测结果：PD-L1 TPS：13% TPS：13%'
    _, _, document, _, _ = fixture(django_user_model, name='literal-other-occurrence', rows=rows)
    first, second = document.facts.filter(field_key='ihc.score').order_by('reading_order')
    other = second.source_fragments.first()
    ordinal = first.source_fragments.count()
    copied = FactSourceFragment.objects.create(
        fact=first, ordinal=ordinal, document_page=other.document_page, evidence=other.evidence,
        ocr_block=other.ocr_block, source_kind='OCR', start_offset=other.start_offset,
        end_offset=other.end_offset, raw_text=other.raw_text, polygon=other.polygon)
    copied.full_clean()
    raw = first.raw_text + '\n' + other.raw_text
    Fact.objects.filter(pk=first.pk).update(raw_text=raw)
    SourceEvidence.objects.filter(pk=first.evidence_id).update(source_text=raw)
    first = declare(Fact.objects.get(pk=first.pk), declaration(value=[ordinal]))
    assert other.raw_text == first.automatic_content['raw_value']
    with pytest.raises(ValidationError):
        validate_context_candidate(first)


def test_declared_score_label_cannot_substitute_for_numeric_value_coverage(django_user_model):
    from apps.processing.models import SourceEvidence

    _, _, document, _, _ = fixture(django_user_model, name='literal-score-prefix')
    score = document.facts.get(field_key='ihc.score', automatic_content__value__score_kind='TPS')
    original = score.source_fragments.first()
    ordinal = score.source_fragments.count()
    evidence = SourceEvidence.objects.create(parsing_version=score.parsing_version,
        document_page=original.document_page, ocr_block=original.ocr_block,
        polygon=original.polygon, source_text='TPS', confidence=.98)
    prefix = FactSourceFragment.objects.create(fact=score, ordinal=ordinal,
        document_page=original.document_page, evidence=evidence, ocr_block=original.ocr_block,
        source_kind='OCR', start_offset=original.start_offset, end_offset=original.start_offset + 3,
        raw_text='TPS', polygon=original.polygon)
    prefix.full_clean()
    raw = score.raw_text + '\nTPS'
    Fact.objects.filter(pk=score.pk).update(raw_text=raw)
    SourceEvidence.objects.filter(pk=score.evidence_id).update(source_text=raw)
    score = declare(Fact.objects.get(pk=score.pk), declaration(value=[ordinal], label=[0]))
    with pytest.raises(ValidationError):
        validate_context_candidate(score)


def test_new_binding_requires_entire_marker_value_not_only_canonical_prefix(django_user_model):
    _, _, document, _, _ = fixture(django_user_model, rows=named_report_rows(), name='binding-full-window')
    score = document.facts.filter(field_key='ihc.score').first()
    marker = document.facts.get(field_key='ihc.marker')
    content = deepcopy(score.automatic_content)
    binding = next(b for b in content['entity_context']['bindings'] if b['role'] == 'MARKER')
    full = score.source_fragments.get(ordinal=binding['proof_fragment_ordinals'][0])
    # Genuine shortened original proof still cannot prove the whole descriptor.
    from apps.processing.models import SourceEvidence
    fragment = FactSourceFragment.objects.create(fact=score, ordinal=score.source_fragments.count(),
        document_page=full.document_page, ocr_block=full.ocr_block, source_kind='OCR',
        start_offset=full.start_offset, end_offset=full.start_offset + 5, raw_text='PD-L1', polygon=full.polygon,
        evidence=SourceEvidence.objects.create(parsing_version=score.parsing_version,
            document_page=full.document_page, ocr_block=full.ocr_block, source_text='PD-L1', polygon=full.polygon, confidence=.98))
    fragment.full_clean()
    raw = score.raw_text + '\nPD-L1'
    Fact.objects.filter(pk=score.pk).update(raw_text=raw)
    SourceEvidence.objects.filter(pk=score.evidence_id).update(source_text=raw)
    binding['proof_fragment_ordinals'] = [fragment.ordinal]
    Fact.objects.filter(pk=score.pk).update(automatic_content=content)
    assert marker.automatic_content['value']['raw'] == 'PD-L1'
    with pytest.raises(ValidationError):
        validate_context_candidate(Fact.objects.get(pk=score.pk))


def test_role_declaration_change_invalidates_confirmation_without_rewriting_raw(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    from apps.facts.revisions import FactConflict, revise_fact
    from tests.facts.pathology_factories import review

    _, patient, document, _, _ = fixture(django_user_model, name='literal-revision-token')
    field = document.facts.get(field_key='specimen.identity')
    original = deepcopy(field.automatic_content)
    review(patient, field)
    field.refresh_from_db()
    assert effective_field(field)['usable']
    token = effective_field(field)['current_source_token']
    content = deepcopy(field.automatic_content)
    content['literal_source']['label_fragment_ordinals'] = []
    Fact.objects.filter(pk=field.pk).update(automatic_content=content)
    fresh = Fact.objects.get(pk=field.pk)
    assert not effective_field(fresh)['usable']
    assert fresh.automatic_content['raw_value'] == original['raw_value']
    with pytest.raises(FactConflict):
        revise_fact(patient, field.pk, actor=patient.account, action='CONFIRM', expected_revision=field.revision_number,
                    expected_source=token, checked_original=True)


def test_undeclared_legacy_automatic_graph_keeps_its_existing_qualification(django_user_model):
    from apps.facts.clinical_readmodels import effective_field
    from tests.facts.pathology_factories import review

    _, patient, document, _, _ = fixture(django_user_model, name='literal-legacy-qualification')
    for field in document.facts.filter(representation='FIELD'):
        content = deepcopy(field.automatic_content)
        content.pop('literal_source', None)
        Fact.objects.filter(pk=field.pk).update(automatic_content=content)
    for field in document.facts.filter(representation='FIELD').order_by('reading_order'):
        validate_context_candidate(field)
        review(patient, field)
    assert all(effective_field(field)['usable'] for field in document.facts.filter(field_key='ihc.score'))


@pytest.mark.parametrize('table', [False, True])
def test_score_label_window_retains_its_printed_kind_without_another_score(django_user_model, table):
    _, _, document, _, _ = fixture(django_user_model, name='score-own-label-' + str(table),
                                  rows=named_report_rows() if table else None)
    for score in document.facts.filter(field_key='ihc.score'):
        literal = proof_text(score, 'literal')
        labels = [p.raw_text for p in score.source_fragments.filter(
            ordinal__in=score.automatic_content['literal_source']['label_fragment_ordinals'])]
        assert literal in labels
        other = 'CPS' if score.automatic_content['value']['score_kind'] == 'TPS' else 'TPS'
        assert all(other not in label for label in labels)


def test_actual_new_role_graph_selected_output_keeps_semantics_without_private_sources(django_user_model):
    from apps.exports.content import assert_snapshot_current, build_snapshot
    from apps.exports.formats import json_bytes, read_structured_data
    from tests.exports.test_pathology_exports import _public_text, selection
    from tests.facts.pathology_factories import review
    from tests.facts.test_pathology_split_metadata import split_rows

    _, patient, document, _, _ = fixture(django_user_model, name='literal-selected-output', rows=split_rows())
    fields = list(document.facts.filter(representation='FIELD').order_by('reading_order'))
    before = {str(f.pk): deepcopy(f.automatic_content) for f in fields}
    for field in fields:
        review(patient, field)
    cps = next(f for f in fields if f.field_key == 'ihc.score' and f.automatic_content['value']['score_kind'] == 'CPS')
    snapshot = build_snapshot(patient, selection(document, cps))
    text = _public_text(snapshot)
    for excluded in ['literal_source', 'fragment_ordinals', 'SYN-SPLIT', '合成组织切片', 'SYN-CLONE',
                     'PD-L1蛋白表达水平', '２０３２年０６月０２日', 'TPS：１７％']:
        assert excluded not in text
    assert 'PD-L1 CPS 29' in text and '单位未印刷' in text
    assert len(read_structured_data(json_bytes(snapshot))['clinical_fields']) == 1
    assert_snapshot_current(patient, snapshot)
    assert {str(f.pk): f.automatic_content for f in document.facts.filter(representation='FIELD')} == before


def test_multiblock_anchor_value_requires_all_original_pieces(django_user_model):
    from tests.facts.test_clinical_segments import block
    rows = [block('病理诊断报告书', order=0, box=(.2, .04, .8, .07)),
            block('标本编号：', order=1, box=(.05, .12, .2, .15)),
            block('SYN-', order=2, box=(.24, .12, .39, .15)),
            block('DUAL', order=3, box=(.4, .12, .53, .15)),
            block('检测项目：PD-L1免疫组化', order=4, box=(.05, .22, .8, .25)),
            block('检测结果：PD-L1 TPS：13%', order=5, box=(.05, .32, .9, .35))]
    _, _, document, _, _ = fixture(django_user_model, name='literal-multiblock', rows=rows)
    anchor = document.facts.get(field_key='specimen.identity')
    assert proof_text(anchor, 'value') == 'SYN-\nDUAL'
    score = document.facts.get(field_key='ihc.score')
    validate_context_candidate(score)
    content = deepcopy(score.automatic_content)
    binding = next(b for b in content['entity_context']['bindings'] if b['role'] == 'SPECIMEN')
    assert len(binding['proof_fragment_ordinals']) == 2
    binding['proof_fragment_ordinals'] = binding['proof_fragment_ordinals'][:1]
    Fact.objects.filter(pk=score.pk).update(automatic_content=content)
    with pytest.raises(ValidationError):
        validate_context_candidate(Fact.objects.get(pk=score.pk))
