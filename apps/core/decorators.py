from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import redirect

from apps.patients.policies import ConsentPolicyConflict, policy_unavailable_response
from apps.patients.services import account_needs_onboarding

from .tenant import get_request_patient


def patient_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not user.is_active:
            raise Http404("No Patient matches the given query.")
        try:
            if account_needs_onboarding(user):
                return redirect("/onboarding/")
        except ConsentPolicyConflict:
            return policy_unavailable_response(request)
        request.patient = get_request_patient(request)
        return view(request, *args, **kwargs)

    return wrapped
