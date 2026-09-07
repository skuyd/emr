from django.http import Http404
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404

from apps.patients.models import Patient
from apps.patients.access import accessible_patients, authorize_patient
from django.core.exceptions import PermissionDenied


class PatientSelectionRequired(Exception):
    pass


def _require_active_user(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_active:
        raise Http404("No Patient matches the given query.")
    return user


def get_request_patient(request):
    user = _require_active_user(request)
    explicit = request.headers.get("X-Patient-ID") or (
        request.POST.get("patient_id") if request.method == "POST" else request.GET.get("patient")
    )
    selected = explicit or getattr(request, "session", {}).get("active_patient_id")
    if selected:
        try:
            return authorize_patient(selected, user).patient
        except (PermissionDenied, ValidationError, ValueError, TypeError):
            raise Http404("No Patient matches the given query.") from None
    patients = list(accessible_patients(user)[:2])
    if len(patients) == 1:
        return patients[0]
    if len(patients) > 1:
        raise PatientSelectionRequired
    raise Http404("No Patient matches the given query.")


def get_patient_object_or_404(queryset, request, id):
    user = _require_active_user(request)
    try:
        return get_object_or_404(queryset.filter(pk__in=accessible_patients(user).values("pk"), pk=id))
    except (TypeError, ValidationError, ValueError):
        raise Http404("No Patient matches the given query.") from None
