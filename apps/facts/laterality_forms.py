"""Human source selections retain original Unicode ranges and explicit pages."""
import re

from django import forms
from django.forms import formset_factory

from .laterality_schema import validate_scoped_value


class ScopeChangeForm(forms.Form):
    parent_id = forms.ChoiceField(label='父位置')
    scope_kind = forms.ChoiceField(label='侧别作用范围', choices=[('NAMED_MEMBERS_ONLY', '仅限列明的部位'),
                                                              ('WHOLE_ENTITY', '适用于完整父位置')])
    expected_revision = forms.IntegerField(required=False, min_value=0, widget=forms.HiddenInput)
    expected_source = forms.CharField(required=False, max_length=64, widget=forms.HiddenInput)
    expected_parent_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_parent_source = forms.CharField(max_length=64, widget=forms.HiddenInput)
    checked_original = forms.BooleanField(label='我已查看原件并核对父位置、列明部位和侧别作用范围')
    confirm = forms.BooleanField(label='同时确认本次新字段与原件一致（不勾选则保持待核对）', required=False)

    def __init__(self, *args, parents=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['parent_id'].choices = [('', '请选择父位置'), *parents]


class ScopeMemberForm(forms.Form):
    site_text = forms.CharField(label='列明的部位', max_length=512, widget=forms.Textarea(attrs={'rows': 2}))
    code = forms.ChoiceField(label='此部位的侧别', choices=[('LEFT', '左'), ('RIGHT', '右'), ('BILATERAL', '双侧'), ('MIDLINE', '中线')])
    sources = forms.MultipleChoiceField(label='所依据的父位置原文（跨段时可多选）', widget=forms.CheckboxSelectMultiple)
    source_mode = forms.ChoiceField(label='来源定位方式', choices=[('OCR', '从选中的原文定位区域'), ('MANUAL_PAGE', '人工对照所选原件页转录')])
    raw_text = forms.CharField(label='此部位的原文', max_length=512, strip=False, widget=forms.Textarea(attrs={'rows': 3}),
        help_text='跨段文字保留换行。原文须能唯一定位；人工转录只保留页定位，不伪造区域。')

    def __init__(self, *args, sources=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['sources'].choices = sources


ScopeMemberFormSet = formset_factory(ScopeMemberForm, extra=0, min_num=1, validate_min=True,
                                    max_num=32, validate_max=True, absolute_max=32, can_delete=True)


def selected_ranges(parent, member_key, values):
    selected = set(values['sources'])
    fragments = [source for source in parent.source_fragments.select_related('ocr_block', 'document_page').order_by('ordinal')
                 if str(source.pk) in selected]
    if len(fragments) != len(selected) or not fragments:
        raise forms.ValidationError('部分所选原文不属于当前父位置，请重新选择。')
    raw, mode = values['raw_text'], values['source_mode']
    if mode == 'MANUAL_PAGE' and len(fragments) == 1:
        return [{'member_key': member_key, 'parent_fragment_id': fragments[0].pk,
                 'page_number': fragments[0].document_page.page_number, 'raw_text': raw}]
    joined = '\n'.join(source.raw_text for source in fragments)
    matches = list(re.finditer('(?=' + re.escape(raw) + ')', joined)) if raw else []
    if len(matches) != 1:
        raise forms.ValidationError('部位原文须在选中片段内准确出现一次；重复时请缩小原文选择或按明确原件页核对。')
    start, end = matches[0].start(), matches[0].start() + len(raw)
    result, cursor = [], 0
    for source in fragments:
        left, right = max(start, cursor), min(end, cursor + len(source.raw_text))
        if left < right:
            if mode == 'OCR':
                if source.source_kind != 'OCR' or source.ocr_block_id is None:
                    raise forms.ValidationError('所选片段没有文字区域，请按原件页人工核对。')
                result.append({'member_key': member_key, 'parent_fragment_id': source.pk,
                               'start_offset': source.start_offset + left - cursor, 'end_offset': source.start_offset + right - cursor})
            else:
                result.append({'member_key': member_key, 'parent_fragment_id': source.pk,
                               'page_number': source.document_page.page_number, 'raw_text': source.raw_text[left-cursor:right-cursor]})
        cursor += len(source.raw_text) + 1
    return result


def build_scope(parent, scope_kind, formset):
    rows = [form.cleaned_data for form in formset.forms if form.cleaned_data and not form.cleaned_data.get('DELETE')]
    if scope_kind == 'WHOLE_ENTITY':
        if len(rows) != 1:
            raise forms.ValidationError('完整部位范围只填写一项，并选择完整父位置原文。')
        row = rows[0]
        return {'code': row['code'], 'raw': row['raw_text']}, selected_ranges(parent, 'whole', row)
    members, ranges = [], []
    for index, row in enumerate(rows, 1):
        key = f'member:{index:03}'
        members.append({'member_key': key, 'site_text': row['site_text'], 'code': row['code'], 'raw': row['raw_text']})
        ranges.extend(selected_ranges(parent, key, row))
    value = {'scope': 'NAMED_MEMBERS_ONLY', 'members': members}
    validate_scoped_value(value)
    return value, ranges
