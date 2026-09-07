from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.responses import protect_sensitive_html
from .access import Capability, accessible_patients, authorize_patient, change_membership
from .forms import DisplayNameForm
from .models import Patient, PatientMembership
from .services import missing_current_consents


def _render(request, template, context=None, status=200):
    return protect_sensitive_html(render(request, template, context or {}, status=status))


@login_required
@require_GET
def patient_list(request):
    return _render(request, "patients/family_list.html", {"patients": accessible_patients(request.user)})


@login_required
@require_http_methods(["GET", "POST"])
def create_patient(request):
    if not request.user.is_active:
        raise PermissionDenied
    if missing_current_consents(request.user):
        return redirect("/onboarding/")
    form = DisplayNameForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        if request.POST.get("upload_authority") != "on":
            form.add_error(None, "请确认有权管理这位患者的资料。")
        else:
            from apps.accounts.models import Account
            with transaction.atomic():
                actor = Account.objects.select_for_update().filter(pk=request.user.pk, is_active=True).first()
                if actor is None:
                    raise PermissionDenied
                patient = Patient.objects.create(account=actor, display_name=form.cleaned_data["display_name"])
                from apps.operations.audit import record_audit_event
                record_audit_event(actor.pk, "patient_created", patient.pk, "succeeded")
            request.session["active_patient_id"] = str(patient.pk)
            return redirect("/")
    return _render(request, "patients/family_create.html", {"form": form}, status=400 if request.method == "POST" else 200)


@login_required
@require_POST
def select_patient(request, patient_id):
    access = authorize_patient(patient_id, request.user)
    request.session["active_patient_id"] = str(access.patient.pk)
    return redirect("/")


@login_required
@require_http_methods(["GET", "POST"])
def members(request, patient_id):
    access = authorize_patient(patient_id, request.user, Capability.MANAGE)
    request.patient, request.patient_access = access.patient, access
    error = ""
    if request.method == "POST":
        try:
            change_membership(access.patient, request.user, request.POST.get("membership_id"),
                              role=request.POST.get("role"), revoke=request.POST.get("action") == "revoke",
                              expected_revision=int(request.POST.get("revision", "")))
            return redirect("patients_family:members", patient_id=patient_id)
        except (ValueError, ValidationError):
            error = "成员已变化或输入无效，请刷新后重试。"
    response = _render(request, "patients/family_members.html", {
        "members": PatientMembership.objects.filter(patient=access.patient, revoked_at__isnull=True).order_by("created_at"),
        "roles": PatientMembership.Role.choices, "error": error,
    }, status=409 if error else 200)
    if request.method == "GET":
        authorize_patient(patient_id, request.user, Capability.MANAGE)
    return response


@login_required
@require_http_methods(["GET", "POST"])
def delete_patient(request, patient_id):
    access = authorize_patient(patient_id, request.user, Capability.OWNER)
    request.patient = access.patient
    if request.method == "POST" and request.POST.get("confirmation") == "delete-patient":
        from .deletion import request_patient_deletion
        from apps.documents.tasks import safe_enqueue_document_deletion
        request_patient_deletion(patient_id, request.user, document_dispatch=safe_enqueue_document_deletion)
        if request.session.get("active_patient_id") == str(patient_id):
            request.session.pop("active_patient_id", None)
        return redirect("patients_family:list")
    response = _render(request, "patients/family_delete.html")
    if request.method == "GET":
        authorize_patient(patient_id, request.user, Capability.OWNER)
    return response
