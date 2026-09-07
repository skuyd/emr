from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.responses import protect_sensitive_html
from .access import Capability, authorize_patient
from .invitation_forms import InvitationForm
from .invitations import InvitationUnavailable, accept_invitation, create_invitation, inspect_invitation, revoke_invitation
from .models import PatientInvitation


def _private(response):
    return protect_sensitive_html(response)


@login_required
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters("recipient_phone")
@sensitive_variables()
def invitations(request, patient_id):
    access = authorize_patient(patient_id, request.user, Capability.MANAGE)
    request.patient, request.patient_access = access.patient, access
    if request.method == "POST" and request.POST.get("role") == "ADMIN" and not access.is_owner:
        raise PermissionDenied
    form = InvitationForm(request.POST if request.method == "POST" else None)
    if not access.is_owner:
        form.fields["role"].choices = [(key, label) for key, label in form.fields["role"].choices if key != "ADMIN"]
    invitation_link, status = "", 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            created = create_invitation(access.patient, request.user, **form.cleaned_data)
            invitation_link = request.build_absolute_uri(reverse("family_invitation:landing")) + "#" + urlencode({"token": created.token})
            status = 201
            form = InvitationForm()
            if not access.is_owner:
                form.fields["role"].choices = [(key, label) for key, label in form.fields["role"].choices if key != "ADMIN"]
    rows = Paginator(PatientInvitation.objects.filter(patient=access.patient).order_by("-created_at", "pk"), 20).get_page(request.GET.get("page"))
    now = timezone.now()
    for row in rows:
        row.display_status = "已撤销" if row.revoked_at else "已接受" if row.accepted_at else "已到期" if row.expires_at <= now else "等待接受"
        if row.display_status == "等待接受":
            try:
                creator = authorize_patient(access.patient, row.created_by_id, Capability.MANAGE)
                if creator.membership.revision != row.creator_revision:
                    row.display_status = "邀请人权限已变化"
            except PermissionDenied:
                row.display_status = "邀请人权限已变化"
        row.can_revoke = row.revoked_at is None and row.accepted_at is None and (access.is_owner or row.role != "ADMIN")
    response = render(request, "patients/invitations.html", {"form": form, "invitations": rows, "invitation_link": invitation_link}, status=status)
    authorize_patient(patient_id, request.user, Capability.MANAGE)
    return _private(response)


@login_required
@require_POST
def revoke(request, patient_id, invitation_id):
    revoke_invitation(patient_id, request.user, invitation_id)
    return redirect("patients_family:invitations", patient_id=patient_id)


@require_GET
def landing(request):
    return _private(render(request, "patients/invitation_landing.html"))


def _login_required_json(request):
    if not request.user.is_authenticated:
        return _private(JsonResponse({"error": "请先登录后继续。", "login_url": reverse("accounts:login") + "?" + urlencode({"next": reverse("family_invitation:landing")})}, status=401))
    return None


@require_POST
@sensitive_post_parameters("token")
@sensitive_variables()
def inspect(request):
    login_response = _login_required_json(request)
    if login_response is not None:
        return login_response
    try:
        invitation, patient = inspect_invitation(request.POST.get("token"), request.user)
    except InvitationUnavailable:
        return _private(JsonResponse({"error": str(InvitationUnavailable())}, status=410))
    return _private(JsonResponse({"patient_name": patient.display_name, "role": invitation.get_role_display(), "expires_at": invitation.expires_at.isoformat()}))


@require_POST
@sensitive_post_parameters("token")
@sensitive_variables()
def accept(request):
    login_response = _login_required_json(request)
    if login_response is not None:
        return login_response
    try:
        result = accept_invitation(request.POST.get("token"), request.user)
    except InvitationUnavailable:
        return _private(JsonResponse({"error": str(InvitationUnavailable())}, status=410))
    if result.already_member:
        return _private(JsonResponse({"error": "你已经是家庭成员。角色保持原样；如需调整，请联系管理者。", "consumed": True}, status=409))
    return _private(JsonResponse({"patient_id": str(result.membership.patient_id), "redirect": "/records/?" + urlencode({"patient": str(result.membership.patient_id)})}))
