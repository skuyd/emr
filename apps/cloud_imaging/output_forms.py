"""Safe labels and original-option conflict tokens for output selection."""
from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.exports.errors import SnapshotChanged
from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient

from .readmodels import source_queryset, source_material


def choice_material(patient, actor):
    with transaction.atomic():
        access=authorize_patient(patient, actor, lock=True)
        rows=[]
        for source in source_queryset().filter(patient=access.patient).order_by('pk'):
            try:
                row=source_material(source)
            except (ValidationError, AttributeError):
                continue
            if row['usable']:
                value=row['id'] + ':' + row['source_token']
                rows.append((value, f"{row['site_label']} · 第 {row['evidence']['page']} 页 · 修订 {row['revision_number']}"))
        return rows


def add_cloud_field(form, patient, actor):
    choices=choice_material(patient, actor)
    form.cloud_choices_token=digest(choices)
    form.fields['cloud_source_ids']=forms.MultipleChoiceField(
        label='云影像来源（仅纳入明确勾选的入口）', required=False,
        choices=choices, widget=forms.CheckboxSelectMultiple,
        error_messages={'invalid_choice':'云影像选项已变化，请重新选择。'},
        help_text='下载的文件可能包含访问参数；仅选择来源不会附带整份原件。')
    selected=set(form.initial.get('cloud_source_ids', []))
    form.initial['cloud_source_ids']=[value for value,_ in choices if value.split(':',1)[0] in selected]


def cloud_selection(cleaned):
    pairs=[value.split(':',1) for value in cleaned.get('cloud_source_ids', [])]
    return {'cloud_source_ids':[identity for identity,_ in pairs], 'cloud_source_tokens':dict(pairs)}


def assert_choices_current(form, patient, actor):
    if form.cloud_choices_token != digest(choice_material(patient, actor)):
        raise SnapshotChanged('云影像选项已变化，请刷新页面后重新选择。')
