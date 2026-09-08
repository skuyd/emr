"""An explicit selection uses the same complete state from form to response."""

import hmac

from django import forms
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse

from apps.core.responses import protect_sensitive_html
from apps.patients.access import authorize_patient

from .exporting import has_selection, selectable_candidate, valid_fingerprint
from .forms import ASSERTION_LABELS, STATUS_LABELS, SUBJECT_LABELS
from .profiles import PROFILE_LABELS
from .readmodels import resolve_ordering


def add_fields(form, patient, *, state=None):
    state = resolve_ordering(patient) if state is None else state
    form.cancer_ordering_state = state
    form.cancer_ordering_label = PROFILE_LABELS[state['profile']]
    form.fields['cancer_candidate_ids'] = forms.MultipleChoiceField(
        label='报告表述（逐条选择）', required=False, widget=forms.CheckboxSelectMultiple,
        choices=[(row['id'], ' · '.join((row['content']['label'], ASSERTION_LABELS[row['content']['assertion']],
                    SUBJECT_LABELS[row['content']['subject']], STATUS_LABELS[row['status']],
                    row['source']['filename'], f"第 {row['source']['page']} 页")))
                 for row in state['candidates'] if selectable_candidate(row)],
        help_text='保留表述的断言、所属对象和核对状态。仅选择这些表述不会自动纳入原件或其他报告内容。')
    form.fields['include_indicator_ordering'] = forms.BooleanField(label='携带当前指标显示偏好', required=False,
        help_text='当前为' + form.cancer_ordering_label + '；仅说明指标排列方式，不代表诊断确认。')
    form.fields['cancer_expected_fingerprint'] = forms.RegexField(regex=r'\A[a-f0-9]{64}\Z', required=False,
                                                               widget=forms.HiddenInput)
    form.initial['cancer_expected_fingerprint'] = state['fingerprint']


def clean_selection(form, data):
    if has_selection(data):
        expected = data.get('cancer_expected_fingerprint')
        if not valid_fingerprint(expected) or not hmac.compare_digest(expected, form.cancer_ordering_state['fingerprint']):
            form.add_error('cancer_expected_fingerprint', '报告表述或显示偏好已变化，请刷新页面后重新选择。')
        if 'cancer_ordering' not in data.get('sections', []):
            form.add_error('sections', '请选择报告表述与显示偏好的展示范围。')
    return data


def changed_response():
    return protect_sensitive_html(HttpResponse('报告来源、核对记录或显示偏好已变化，请刷新后重新选择。', status=409))


def finish_response(response, patient, actor, capability, state):
    try:
        authorize_patient(patient, actor, capability)
        current = resolve_ordering(patient)
    except ObjectDoesNotExist:
        response.close()
        return changed_response()
    except Exception:
        response.close()
        raise
    if current['fingerprint'] != state['fingerprint']:
        response.close()
        return changed_response()
    return protect_sensitive_html(response)
