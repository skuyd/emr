"""Safe labels and original-option conflict tokens for output selection."""
from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from apps.exports.errors import SnapshotChanged
from apps.facts.readmodels import digest
from apps.patients.access import authorize_patient

from .readmodels import source_queryset, source_material
from .projection import project_default_snapshot


def _short_identity(identity, identities):
    length = 8
    while any(other != identity and other.startswith(identity[:length]) for other in identities):
        length += 1
    return identity[:length]


def choice_material(patient, actor):
    with transaction.atomic():
        access=authorize_patient(patient, actor, lock=True)
        current=[]
        for source in source_queryset().filter(patient=access.patient).order_by('created_at','pk'):
            try:
                row=source_material(source)
            except (ValidationError, AttributeError):
                continue
            if row['usable']:
                current.append((source,row))
        identities={row['id'] for _,row in current}
        rows=[]
        page_counts={}
        for source,row in current:
            page_key=(row['document_id'],row['evidence']['page_id'])
            page_counts[page_key]=page_counts.get(page_key,0)+1
            labels=project_default_snapshot({'filename':source.document.display_filename,
                'title':source.title or (source.report.title if source.report_id else '')})
            value=row['id'] + ':' + row['source_token']
            rows.append((value, f"{labels['filename']} · {labels['title'] or '云影像来源'} · {row['site_label']} · "
                f"第 {row['evidence']['page']} 页 · 本页第 {page_counts[page_key]} 个来源 · "
                f"{source.evidence.get_kind_display()} · 修订 {row['revision_number']} · 来源 {_short_identity(row['id'],identities)}"))
        return rows


def add_cloud_field(form, patient, actor):
    choices=choice_material(patient, actor)
    form.cloud_choices_token=digest(choices)
    identities={value.split(':',1)[0] for value,_ in choices}
    links=format_html_join('；', '<a href="{}?patient={}" target="_blank" rel="noopener">核对来源 {} 与原页</a>', (
        (reverse('cloud_imaging:source',args=[identity]),str(patient.pk),_short_identity(identity,identities))
        for identity in sorted(identities)))
    form.fields['cloud_source_ids']=forms.MultipleChoiceField(
        label='云影像来源（仅纳入明确勾选的入口）', required=False,
        choices=choices, widget=forms.CheckboxSelectMultiple,
        error_messages={'invalid_choice':'云影像选项已变化，请重新选择。'},
        help_text=format_html('下载的文件可能包含访问参数；仅选择来源不会附带整份原件。{}',links))
    selected=set(form.initial.get('cloud_source_ids', []))
    form.initial['cloud_source_ids']=[value for value,_ in choices if value.split(':',1)[0] in selected]


def cloud_selection(cleaned):
    pairs=[value.split(':',1) for value in cleaned.get('cloud_source_ids', [])]
    return {'cloud_source_ids':[identity for identity,_ in pairs], 'cloud_source_tokens':dict(pairs)}


def assert_choices_current(form, patient, actor):
    if form.cloud_choices_token != digest(choice_material(patient, actor)):
        raise SnapshotChanged('云影像选项已变化，请刷新页面后重新选择。')
