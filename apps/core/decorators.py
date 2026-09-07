from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponse
from django.shortcuts import redirect

from apps.patients.policies import ConsentPolicyConflict, policy_unavailable_response
from apps.patients.services import account_needs_onboarding

from .tenant import get_request_patient, PatientSelectionRequired
from apps.patients.access import Capability, accessible_patients, authorize_patient


def patient_required(view=None, *, capability=None):
    if view is None:
        return lambda candidate: patient_required(candidate, capability=capability)
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not user.is_active:
            raise Http404("No Patient matches the given query.")
        try:
            if account_needs_onboarding(user):
                if kwargs:
                    raise Http404("No Patient matches the given query.")
                return redirect("/onboarding/")
        except ConsentPolicyConflict:
            return policy_unavailable_response(request)
        if not kwargs and not accessible_patients(user).exists():
            return redirect("patients_family:create")
        try:
            request.patient = get_request_patient(request)
        except PatientSelectionRequired:
            return redirect("patients_family:list")
        required = capability or (Capability.WRITE if request.method in {"POST", "PUT", "PATCH", "DELETE"} else Capability.READ)
        request.patient_access = authorize_patient(request.patient, user, required)
        resolved_patient_id = request.patient.pk
        if (request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and not (request.headers.get("X-Patient-ID") or request.POST.get("patient_id"))
                and accessible_patients(user).count() > 1):
            return HttpResponse("患者选择已变化，请刷新页面后重试。", status=409)
        response = view(request, *args, **kwargs)
        if request.method in {"GET", "HEAD"} and response.status_code < 400:
            try:
                # A read may render while another request revokes membership.
                # Recheck the same resolved archive before releasing its body.
                authorize_patient(resolved_patient_id, user, required)
            except Exception:
                response.close()
                raise
        selected = getattr(request, "session", {}).get("active_patient_id")
        if (response.status_code in {301, 302, 303, 307, 308} and str(selected) != str(request.patient.pk)
                and (selected or accessible_patients(user).count() > 1)):
            target = urlsplit(response.get("Location", ""))
            if not target.scheme and not target.netloc and target.path.startswith("/"):
                query = [(key, value) for key, value in parse_qsl(target.query, keep_blank_values=True) if key != "patient"]
                query.append(("patient", str(request.patient.pk)))
                response["Location"] = urlunsplit(("", "", target.path, urlencode(query), target.fragment))
        return response

    wrapped.patient_scoped = True
    return wrapped
