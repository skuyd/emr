"""Reported member qualifiers retain their own original source and parent site."""
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.clinical_schema import field_content, validate_value
from apps.facts.clinical_services import add_manual_clinical_field
from apps.facts.clinical_readmodels import report_source_token
from apps.facts.models import FactRevision
from apps.facts.readmodels import effective_fact
from apps.facts.revisions import revise_fact
from tests.facts.test_clinical_foundation import clinical_fixture
from tests.facts.test_imaging_quantitative import imaging, fields


pytestmark = pytest.mark.django_db


def named_value(*items):
    return {'scope': 'NAMED_MEMBERS_ONLY', 'members': [
        {'member_key': f'member:{i:03}', 'site_text': text, 'code': code, 'raw': text}
        for i, (text, code) in enumerate(items, 1)
    ]}


def confirm(patient, field, action='CONFIRM', **kwargs):
    field.refresh_from_db()
    row = effective_fact(field)
    return revise_fact(patient, field.pk, actor=patient.account, action=action,
                       expected_revision=field.revision_number, expected_source=row['current_source_token'],
                       checked_original=action in {'CONFIRM', 'CORRECT'}, **kwargs)


def test_scoped_schema_keeps_ordered_named_members_and_explicit_display():
    value = named_value(('左肺', 'LEFT'), ('右肾', 'RIGHT'))
    assert validate_value('lesion.scoped_laterality', value) == value
    content = field_content('lesion.scoped_laterality', value, '左肺及右肾')
    assert content['value_type'] == 'SCOPED_LATERALITY'
    assert content['value'] == value
    assert '左肺' in content['text'] and '右肾' in content['text'] and '仅限列明部位' in content['text']


@pytest.mark.parametrize('change', ['empty', 'duplicate', 'unknown', 'whole', 'extra'])
def test_scoped_schema_rejects_erased_or_ambiguous_members(change):
    value = named_value(('双肺门', 'BILATERAL'))
    if change == 'empty':
        value['members'] = []
    elif change == 'duplicate':
        value['members'] *= 2
    elif change == 'unknown':
        value['members'][0]['code'] = 'UNKNOWN'
    elif change == 'whole':
        value['scope'] = 'WHOLE_ENTITY'
    else:
        value['unselected_parent_text'] = '合成其他位置'
    with pytest.raises(ValidationError):
        validate_value('lesion.scoped_laterality', value)


@pytest.mark.parametrize(('body', 'expected'), [
    ('纵隔及双肺门见淋巴结，短径12mm。', [('双肺门', 'BILATERAL')]),
    ('左肺及右肾见结节。', [('左肺', 'LEFT'), ('右肾', 'RIGHT')]),
    ('肝内及右肺见结节。', [('右肺', 'RIGHT')]),
])
def test_mixed_group_persists_only_named_qualifiers_with_real_parent_ranges(django_user_model, body, expected):
    _, _, document, _, run = imaging(django_user_model, body)
    parent = fields(document, 'lesion.site')[0]
    child = document.facts.get(field_key='lesion.scoped_laterality')
    assert run.status == 'EXTRACTED'
    assert not document.facts.filter(field_key='lesion.laterality').exists()
    assert child.automatic_content['value'] == named_value(*expected)
    binding = child.laterality_scope_binding
    assert binding.parent_site_id == parent.pk and binding.original_parent_id == parent.pk
    assert binding.scope_kind == 'NAMED_MEMBERS_ONLY' and binding.origin == 'AUTOMATIC'
    assert binding.created_by_id is None and binding.original_created_by_id is None
    row = effective_fact(child)
    assert row['status'] == 'PENDING' and row['source_valid'] and not row['usable']
    assert row['laterality_scope']['scope_state'] == 'NAMED_MEMBERS_ONLY'
    assert row['laterality_scope']['parent_id'] == str(parent.pk)
    assert len(binding.ranges.all()) == len(expected)
    for source in binding.ranges.select_related('parent_fragment__ocr_block', 'child_fragment'):
        block = source.parent_fragment.ocr_block
        assert source.parent_fragment.fact_id == parent.pk and source.child_fragment.fact_id == child.pk
        assert source.raw_text == block.text[source.start_offset:source.end_offset]
        assert source.reading_order == block.reading_order and source.polygon == block.polygon
        assert source.parent_fragment.start_offset <= source.start_offset < source.end_offset <= source.parent_fragment.end_offset
        assert source.raw_text in {text for text, _ in expected}
    assert not FactRevision.objects.filter(fact__document=document).exists()


@pytest.mark.parametrize(('site', 'side'), [('左肺', 'LEFT'), ('右肾', 'RIGHT'), ('双肺', 'BILATERAL'),
                                         ('左肺及右肺', 'BILATERAL')])
def test_new_whole_side_has_independent_scope_proof_without_changing_old_value_shape(django_user_model, site, side):
    _, _, document, _, _ = imaging(django_user_model, f'{site}见结节，长径12mm。')
    child = document.facts.get(field_key='lesion.laterality')
    assert child.automatic_content['value'] == {'code': side, 'raw': site}
    assert child.schema_version == '1.0'
    binding = getattr(child, 'laterality_scope_binding', None)
    assert binding is not None, 'New whole-side candidates need explicit original scope proof'
    assert binding.scope_kind == 'WHOLE_ENTITY'
    assert effective_fact(child)['laterality_scope']['scope_state'] == 'WHOLE_ENTITY'
    assert not document.facts.filter(field_key='lesion.scoped_laterality').exists()


def test_named_member_crosses_ocr_blocks_using_original_unicode_offsets(django_user_model):
    texts = ['CT诊断报告书', '影像表现：Ⅲ、纵隔及双', '肺门见淋巴结，短径１２ｍｍ。', '诊断意见：请核对原件。']
    _, _, document, version, _ = clinical_fixture(django_user_model, texts=texts, name='scoped-original-unicode')
    child = document.facts.get(field_key='lesion.scoped_laterality')
    value = child.automatic_content['value']['members'][0]
    assert value == {'member_key': 'member:001', 'site_text': '双\n肺门', 'raw': '双\n肺门', 'code': 'BILATERAL'}
    ranges = list(child.laterality_scope_binding.ranges.order_by('ordinal'))
    assert [(r.start_offset, r.end_offset, r.raw_text) for r in ranges] == [(len(texts[1]) - 1, len(texts[1]), '双'), (0, 2, '肺门')]
    assert [r.reading_order for r in ranges] == [1, 2]
    assert list(version.ocr_blocks.order_by('reading_order').values_list('text', flat=True)) == texts


def test_legacy_confirmation_preserves_scalar_and_history_without_creating_scope(django_user_model):
    _, patient, document, _, _ = imaging(django_user_model, '纵隔及双肺门见淋巴结。')
    report = document.clinical_reports.get()
    parent = document.facts.get(field_key='lesion.site')
    legacy = add_manual_clinical_field(patient, actor=patient.account, report_id=report.pk,
        entity_key=parent.entity_key, field_key='lesion.laterality', value={'code': 'BILATERAL', 'raw': '双肺门'},
        fragments=[{'page_number': 1, 'raw_text': '纵隔及双肺门见淋巴结。'}], expected_report_source=report_source_token(report))
    original = deepcopy(legacy.automatic_content)
    confirm(patient, legacy)
    row = effective_fact(legacy)
    assert row['usable'] and row['content'] == original
    assert row['laterality_scope']['scope_state'] == 'UNKNOWN_SCOPE'
    assert getattr(legacy, 'laterality_scope_binding', None) is None
    legacy.refresh_from_db()
    assert legacy.automatic_content == original and legacy.revisions.count() == 1
