from django import forms

from apps.patients.access import owner_actor
from .readmodels import review_observations


def add_lesion_field(form, patient, *, actor=None):
    rows = review_observations(patient, actor=owner_actor(patient, actor))
    names = {row['lesion_id']: row['lesion_name'] for row in rows if row['usable']}
    form.fields['lesion_ids'] = forms.MultipleChoiceField(
        label='选定人工确认的病灶观察分组', choices=sorted(names.items()), required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text='请同时选择所需报告或字段，至少包含对应部位。此选项不自动增加原件、其他观察、日期或测量。')
