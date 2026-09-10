"""Keep one ordering decision across a rendered comparison or selector."""

from functools import wraps

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse

from apps.core.responses import protect_sensitive_html
from apps.patients.access import authorize_patient, Capability

from .profiles import PROFILE_LABELS
from .readmodels import resolve_ordering


def _changed():
    return protect_sensitive_html(HttpResponse('报告来源或指标显示顺序已变化，请刷新后查看。', status=409))


def ordering_required(view):
    """Use inside patient_required on read-only HTML routes."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            state = resolve_ordering(request.patient)
            request.indicator_ordering = state
            request.indicator_ordering_label = PROFILE_LABELS[state['profile']]
            response = view(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return _changed()
        try:
            authorize_patient(request.patient, request.user, Capability.READ)
            current = resolve_ordering(request.patient)
        except ObjectDoesNotExist:
            response.close()
            return _changed()
        except Exception:
            response.close()
            raise
        if current['fingerprint'] != state['fingerprint']:
            response.close()
            return _changed()
        return protect_sensitive_html(response)
    return wrapped
