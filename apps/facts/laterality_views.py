"""Normal patient-authorized original review; no share-grant fallback."""
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_http_methods

from apps.core.decorators import patient_required
from apps.patients.access import Capability

from .laterality_forms import ScopeChangeForm, ScopeMemberFormSet, build_scope
from .laterality_services import (add_laterality_scope, attest_whole_laterality, operation_material,
                                  replace_laterality_scope, undo_laterality_scope)
from .models import Fact, LateralityScopeBinding, LateralityScopeOperation
from .readmodels import effective_fact, fact_queryset
from .revisions import FactConflict
from .views import _render


def _initial_members(subject, parent):
    if parent is None:
        return []
    row = effective_fact(subject)
    binding = LateralityScopeBinding.objects.filter(fact=subject, parent_site=parent).first()
    if binding:
        value = row['content']['value']
        members = value.get('members') or [{**member, 'code': value['code']} for member in binding.members]
        result = []
        for member in members:
            ranges = list(binding.ranges.filter(member_key=member['member_key']).order_by('ordinal'))
            result.append({'site_text': member['site_text'], 'code': member['code'], 'raw_text': member['raw'],
                           'sources': [str(source.parent_fragment_id) for source in ranges],
                           'source_mode': 'OCR' if ranges and all(source.source_kind == 'OCR' for source in ranges) else 'MANUAL_PAGE'})
        return result
    if subject.field_key == 'lesion.laterality':
        return [{'site_text': effective_fact(parent)['content']['value']['text'], 'code': row['content']['value']['code'],
                 'raw_text': effective_fact(parent)['content']['value']['text'], 'source_mode': 'OCR' if parent.parsing_version_id else 'MANUAL_PAGE'}]
    return []


@patient_required
@require_http_methods(['GET', 'HEAD', 'POST'])
def scope_change(request, fact_id):
    subject = get_object_or_404(fact_queryset(), pk=fact_id, document__patient=request.patient, document__deleted_at__isnull=True)
    if subject.representation != 'FIELD' or subject.field_key not in {'lesion.site', 'lesion.laterality', 'lesion.scoped_laterality'}:
        raise Http404
    row = effective_fact(subject)
    parents = list(fact_queryset().filter(clinical_report_id=subject.clinical_report_id, entity_key=subject.entity_key, field_key='lesion.site'))
    choices = [(str(parent.pk), effective_fact(parent)['content']['value']['text']) for parent in parents]
    binding = LateralityScopeBinding.objects.filter(fact=subject).first()
    preferred = request.POST.get('parent_id') if request.method == 'POST' else request.GET.get('parent_id')
    preferred = preferred or (str(subject.pk) if subject.field_key == 'lesion.site' else
                              str(binding.parent_site_id) if binding and binding.parent_site_id else
                              str(parents[0].pk) if len(parents) == 1 else None)
    parent = next((item for item in parents if str(item.pk) == preferred), None)
    parent_row = effective_fact(parent) if parent else None
    is_add = subject.field_key == 'lesion.site'
    initial = {'parent_id': preferred, 'scope_kind': 'WHOLE_ENTITY' if subject.field_key == 'lesion.laterality' else 'NAMED_MEMBERS_ONLY',
               'expected_revision': None if is_add else row['revision_number'], 'expected_source': '' if is_add else row['current_source_token'],
               'expected_parent_revision': parent_row['revision_number'] if parent_row else None,
               'expected_parent_source': parent_row['current_source_token'] if parent_row else ''}
    sources = list(parent.source_fragments.select_related('document_page').order_by('ordinal')) if parent else []
    source_choices = [(str(source.pk), f'第{source.document_page.page_number}页 · {source.raw_text[:240]}') for source in sources]
    data = request.POST if request.method == 'POST' else None
    form = ScopeChangeForm(data, parents=choices, initial=initial)
    members = ScopeMemberFormSet(data, prefix='members', initial=_initial_members(subject, parent), form_kwargs={'sources': source_choices})
    error, status = '', 200
    if request.method == 'POST':
        if form.is_valid() and members.is_valid() and parent:
            try:
                values = form.cleaned_data
                value, ranges = build_scope(parent, values['scope_kind'], members)
                action = request.POST.get('action')
                common = dict(actor=request.user, parent_id=parent.pk, expected_parent_revision=values['expected_parent_revision'],
                              expected_parent_source=values['expected_parent_source'], ranges=ranges,
                              checked_original=values['checked_original'], confirm=values['confirm'])
                if is_add and action == 'ADD':
                    event = add_laterality_scope(request.patient, scope_kind=values['scope_kind'], value=value, **common)
                elif not is_add and action in {'REPLACE', 'ATTEST'}:
                    common.update(fact_id=subject.pk, expected_revision=values['expected_revision'], expected_source=values['expected_source'])
                    if action == 'ATTEST':
                        if (values['scope_kind'] != 'WHOLE_ENTITY' or subject.field_key != 'lesion.laterality'
                                or value['code'] != row['content']['value']['code']):
                            raise ValidationError('完整范围核对保留原侧别值；更正内容请使用范围替换。')
                        event = attest_whole_laterality(request.patient, **common)
                    else:
                        event = replace_laterality_scope(request.patient, scope_kind=values['scope_kind'], value=value, **common)
                else:
                    raise ValidationError('请选择当前字段支持的范围操作。')
                return redirect('facts:scope_operation', scope_operation_id=event.pk)
            except (ValidationError, FactConflict) as exc:
                error = '；'.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                status = 400 if isinstance(exc, ValidationError) else 409
        else:
            status = 400
    return _render(request, 'facts/scope_change.html', {'fact': subject, 'row': row, 'parent': parent, 'parent_row': parent_row,
        'parent_choices': choices, 'sources': sources, 'form': form, 'formset': members, 'is_add': is_add,
        'can_attest': row.get('laterality_scope', {}).get('scope_state') == 'UNKNOWN_SCOPE',
        'error': error, 'can_write': request.patient_access.permits(Capability.WRITE)}, status=status)


@patient_required
@require_http_methods(['GET', 'HEAD', 'POST'])
def scope_operation(request, scope_operation_id):
    event = get_object_or_404(LateralityScopeOperation.objects.select_related('document', 'old_fact', 'new_fact', 'author'),
                              pk=scope_operation_id, patient=request.patient, document__deleted_at__isnull=True)
    operation = operation_material(request.patient, actor=request.user, operation_id=event.pk)
    error, status = '', 200
    if request.method == 'POST':
        try:
            if request.POST.get('action') != 'UNDO':
                raise ValidationError('请选择撤销范围操作。')
            undo_laterality_scope(request.patient, actor=request.user, operation_id=event.pk,
                                 expected_operation=request.POST.get('expected_operation'))
            return redirect('facts:scope_operation', scope_operation_id=event.pk)
        except (ValidationError, FactConflict) as exc:
            error = '；'.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            status = 400 if isinstance(exc, ValidationError) else 409
    return _render(request, 'facts/scope_operation.html', {'document': event.document, 'event': event, 'operation': operation,
        'error': error, 'revision_links': event.revision_links.select_related('revision__fact').order_by('ordinal'),
        'can_write': request.patient_access.permits(Capability.WRITE)}, status=status)
