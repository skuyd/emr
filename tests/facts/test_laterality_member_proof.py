"""Each named site must occur in its own source, not elsewhere in its parent."""
from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError

from apps.facts.models import Fact, LateralityScopeOperation
from apps.facts.readmodels import effective_fact
from tests.facts.test_laterality_review_guards import fixture
from tests.facts.test_laterality_scope_operations import new_member, replace
from tests.facts.test_scoped_laterality import confirm


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('mode', ['OCR', 'MANUAL_PAGE'])
@pytest.mark.parametrize('own_words', [False, True])
def test_named_member_rejects_coordinated_raw_and_source_substitution(django_user_model, mode, own_words):
    patient, _, parent, child = fixture(django_user_model, f'member-proof-{mode}-{own_words}')
    confirm(patient, parent)
    value, ranges = new_member(parent, child)
    value = deepcopy(value)
    raw = '双肺门' if own_words else '淋巴结'
    value['members'][0]['raw'] = raw
    source = child.laterality_scope_binding.ranges.get().parent_fragment
    if mode == 'OCR':
        start = source.ocr_block.text.index(raw)
        ranges[0].update(start_offset=start, end_offset=start + len(raw))
    else:
        ranges = [{'member_key': 'member:001', 'parent_fragment_id': source.pk,
                   'page_number': source.document_page.page_number, 'raw_text': raw}]
    before = list(Fact.objects.order_by('pk').values_list('pk', 'revision_number'))
    operations = LateralityScopeOperation.objects.count()
    if not own_words:
        with pytest.raises(ValidationError):
            replace(patient, parent, child, value=value, ranges=ranges, confirm_new=True)
        assert list(Fact.objects.order_by('pk').values_list('pk', 'revision_number')) == before
        assert LateralityScopeOperation.objects.count() == operations
    else:
        event = replace(patient, parent, child, value=value, ranges=ranges, confirm_new=True)
        assert effective_fact(event.new_fact)['usable']
        proof = event.new_fact.laterality_scope_binding.ranges.get()
        assert proof.raw_text == '双肺门' and proof.source_kind == mode
        if mode == 'MANUAL_PAGE':
            assert proof.block_id_at_creation is None
            assert proof.start_offset is proof.end_offset is proof.polygon is None


def test_pre_fix_manual_binding_cannot_remain_usable_or_be_confirmed_again(django_user_model):
    """Represent a previously accepted row; reads must not rewrite its history."""
    from apps.exports.content import build_snapshot
    from apps.exports.errors import ExportInputError
    from apps.facts.clinical_schema import field_content
    from apps.facts.models import FactRevision, FactSourceFragment, LateralityScopeBinding, LateralityScopeRange
    from apps.facts.readmodels import source_token
    from apps.facts.revisions import FactConflict

    patient, report, parent, child = fixture(django_user_model, 'pre-fix-manual-member-proof')
    confirm(patient, parent)
    value, _ = new_member(parent, child)
    source = child.laterality_scope_binding.ranges.get().parent_fragment
    event = replace(patient, parent, child, value=value, confirm_new=True,
        ranges=[{'member_key': 'member:001', 'parent_fragment_id': source.pk,
                 'page_number': source.document_page.page_number, 'raw_text': '双肺门'}])
    bad = deepcopy(value)
    bad['members'][0]['raw'] = '淋巴结'
    content = field_content('lesion.scoped_laterality', bad, '淋巴结')
    # Synthetic persisted pre-fix input, not an allowed mutation API. The old
    # service accepted this same coordinated transcription and member mismatch.
    Fact.objects.filter(pk=event.new_fact_id).update(raw_text='淋巴结', automatic_content=content)
    FactSourceFragment.objects.filter(fact_id=event.new_fact_id).update(raw_text='淋巴结')
    LateralityScopeBinding.objects.filter(fact_id=event.new_fact_id).update(members=bad['members'])
    LateralityScopeRange.objects.filter(binding__fact_id=event.new_fact_id).update(raw_text='淋巴结')
    field = Fact.objects.get(pk=event.new_fact_id)
    revision = field.revisions.latest('sequence')
    after = {**revision.after, 'content': content}
    FactRevision.objects.filter(pk=revision.pk).update(after=after)
    after['source_token'] = source_token(field)
    FactRevision.objects.filter(pk=revision.pk).update(after=after)
    before = list(field.revisions.values())
    current = effective_fact(field)
    assert not current['usable'] and not current['source_valid']
    with pytest.raises(FactConflict):
        confirm(patient, field)
    with pytest.raises(ExportInputError):
        build_snapshot(patient, {'mode': 'documents', 'document_ids': [str(report.document_id)],
                                 'clinical_field_ids': [str(field.pk)]})
    assert list(field.revisions.values()) == before
    field.refresh_from_db()
    assert field.automatic_content == content
    replacement = replace(patient, parent, field, value=value, confirm_new=True,
        ranges=[{'member_key': 'member:001', 'parent_fragment_id': source.pk,
                 'page_number': source.document_page.page_number, 'raw_text': '双肺门'}])
    assert effective_fact(replacement.new_fact)['usable']
    field.refresh_from_db()
    assert field.automatic_content == content
