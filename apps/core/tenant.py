from django.http import Http404
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404

from apps.patients.models import Patient


def _require_active_user(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_active:
        raise Http404("No Patient matches the given query.")
    return user


def get_request_patient(request):
    user = _require_active_user(request)
    return get_object_or_404(Patient.objects.filter(account_id=user.pk))


def get_patient_object_or_404(queryset, request, id):
    user = _require_active_user(request)
    try:
        return get_object_or_404(queryset.filter(account_id=user.pk, pk=id))
    except (TypeError, ValidationError, ValueError):
        raise Http404("No Patient matches the given query.") from None
