from django import forms

from .profiles import PROFILE_LABELS


MODE_LABELS = {'AUTO': '根据报告自动排列', 'GENERAL': '通用顺序',
               'CANDIDATE': '使用一条报告表述', 'MANUAL_PROFILE': '手动选择显示顺序'}
ASSERTION_LABELS = {'AFFIRMED': '明确肯定', 'NEGATED': '明确否定', 'UNCERTAIN': '疑似或待排', 'UNKNOWN': '不明确'}
SUBJECT_LABELS = {'CURRENT_PRIMARY': '患者当前原发病', 'METASTATIC_SITE': '转移部位',
                  'HISTORICAL': '既往病史', 'OTHER_PERSON': '其他人', 'UNKNOWN': '所属对象不明确'}
ACTION_LABELS = {'CONFIRM': '与原件一致', 'CORRECT': '更正表述', 'EXCLUDE': '排除此表述',
                 'DEFER': '暂不处理', 'REVOKE': '撤销确认', 'UNDO': '撤销上次操作'}
STATUS_LABELS = {'PENDING': '待核对', 'CONFIRMED': '已核对原件', 'EXCLUDED': '已排除', 'DEFERRED': '暂不处理'}


def selectable(row):
    return (row['source_valid'] and not row['source_changed'] and row['status'] not in {'EXCLUDED', 'DEFERRED'}
            and row['content']['profile'] in {'LUNG', 'PANCREAS'}
            and row['content']['assertion'] == 'AFFIRMED' and row['content']['subject'] == 'CURRENT_PRIMARY')


class SelectionGuardForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_fingerprint = forms.RegexField(regex=r'^[a-f0-9]{64}$', widget=forms.HiddenInput)

    def __init__(self, *args, state, **kwargs):
        super().__init__(*args, initial={'expected_revision': state['revision_number'],
            'expected_fingerprint': state['fingerprint'], **kwargs.pop('initial', {})}, **kwargs)


class SelectionForm(SelectionGuardForm):
    mode = forms.ChoiceField(label='排列方式', choices=list(MODE_LABELS.items()))
    profile = forms.ChoiceField(label='手动显示顺序', required=False,
        choices=[('', '仅手动模式需要选择'), ('LUNG', PROFILE_LABELS['LUNG']), ('PANCREAS', PROFILE_LABELS['PANCREAS'])])
    candidate_id = forms.ChoiceField(label='作为显示依据的报告表述', choices=[], required=False,
        help_text='只用于本档案的指标顺序；选择后仍保留表述的原有核对状态。')

    def __init__(self, *args, state, **kwargs):
        super().__init__(*args, state=state, initial={key: state['selection_state'].get(key) or ''
            for key in ('mode', 'profile', 'candidate_id')}, **kwargs)
        self.fields['candidate_id'].choices = [('', '仅使用报告表述时需要选择')] + [
            (row['id'], f"{row['content']['label']} · {row['source']['filename']} · 第{row['source']['page']}页")
            for row in state['candidates'] if selectable(row)]

    def clean(self):
        data = super().clean()
        mode = data.get('mode')
        if mode == 'MANUAL_PROFILE' and not data.get('profile'):
            self.add_error('profile', '请选择手动显示顺序。')
        if mode == 'CANDIDATE' and not data.get('candidate_id'):
            self.add_error('candidate_id', '请选择当前可用的报告表述。')
        # Other visible selectors can retain their submitted values while the
        # selected mode determines which explicit value the service receives.
        return data


class CandidateForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    expected_source = forms.RegexField(regex=r'^[a-f0-9]{64}$', widget=forms.HiddenInput)
    action = forms.ChoiceField(label='本次操作', choices=list(ACTION_LABELS.items()))
    checked_original = forms.BooleanField(label='我已打开原件并核对这条表述及所属对象', required=False)
    label = forms.CharField(label='更正后的完整表述', required=False, max_length=160)
    profile = forms.ChoiceField(label='更正后的指标顺序', required=False,
        choices=[('', '尚无对应顺序'), ('LUNG', PROFILE_LABELS['LUNG']), ('PANCREAS', PROFILE_LABELS['PANCREAS'])])
    assertion = forms.ChoiceField(label='这句话的断言', choices=list(ASSERTION_LABELS.items()), required=False)
    subject = forms.ChoiceField(label='这句话的所属对象', choices=list(SUBJECT_LABELS.items()), required=False)
    reason = forms.CharField(label='操作说明（更正时必填）', required=False, max_length=1000,
                             widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, *args, row, **kwargs):
        initial = {'expected_revision': row['revision_number'], 'expected_source': row['current_source_token'],
                   'action': 'CONFIRM', **{key: row['content'][key] or '' for key in ('label', 'profile', 'assertion', 'subject')}}
        super().__init__(*args, initial=initial, **kwargs)

    def clean(self):
        data = super().clean()
        if data.get('action') in {'CONFIRM', 'CORRECT'} and not data.get('checked_original'):
            self.add_error('checked_original', '请先打开原件并核对，再勾选此项。')
        if data.get('action') == 'CORRECT':
            for key in ('label', 'assertion', 'subject', 'reason'):
                if not data.get(key):
                    self.add_error(key, '更正时请填写此项。')
        return data

    def changes(self):
        if self.cleaned_data['action'] != 'CORRECT':
            return None
        return {key: self.cleaned_data[key] or None for key in ('label', 'profile', 'assertion', 'subject')}
