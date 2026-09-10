from contextlib import closing
from functools import wraps
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.responses import protect_sensitive_html
from apps.core.streams import GuardedStream, guarded_file_response
from apps.documents.backends import get_object_store
from apps.documents.errors import UploadDomainError
from apps.documents.models import Document
from apps.documents.previews import PreviewUnavailable, render_page, render_thumbnail_sheet
from apps.exports.content import SECTIONS
from apps.exports.errors import ExportInputError, SnapshotChanged
from .access import Capability, authorize_patient
from .models import PatientShare
from .share_forms import ShareForm
from .sharing import ShareUnavailable, authorize_share, create_share, exchange_share_token, revoke_share, validate_managed_share


def _private(response):
    return protect_sensitive_html(response)


@login_required
@require_http_methods(["GET", "POST"])
@sensitive_variables()
def shares(request, patient_id):
    access = authorize_patient(patient_id, request.user, Capability.MANAGE)
    request.patient, request.patient_access = access.patient, access
    form = ShareForm(access.patient, request.POST if request.method == "POST" else None, actor=request.user)
    link, status = "", 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                result = create_share(access.patient, request.user, form.selection(), expires_in_hours=form.cleaned_data["expires_in_hours"] or 24,
                    allow_original_download=form.cleaned_data["allow_original_download"])
            except (ExportInputError, SnapshotChanged) as error:
                form.add_error(None, str(error))
            else:
                link = request.build_absolute_uri(reverse("shared:open")) + "#" + urlencode({"token": result.token})
                form, status = ShareForm(access.patient, actor=request.user), 201
    page = Paginator(PatientShare.objects.filter(patient=access.patient).order_by("-created_at", "pk"), 20).get_page(request.GET.get("page"))
    page.object_list = [validate_managed_share(access.patient, request.user, row.pk) for row in page.object_list]
    for share in page:
        share.section_labels = [title for key, title in SECTIONS if key in share.scope.get("sections", [])]
    response = render(request, "patients/shares.html", {"form": form, "share_link": link, "page": page}, status=status)
    authorize_patient(patient_id, request.user, Capability.MANAGE)
    from apps.exports.pathology import selection_unchanged
    if not selection_unchanged(access.patient, form.pathology_stamp):
        response.close()
        return _private(render(request, "patients/share_unavailable.html", status=409))
    from apps.cloud_imaging.output_forms import assert_choices_current
    try:
        assert_choices_current(form, access.patient, request.user)
    except SnapshotChanged:
        response.close()
        return _private(HttpResponse('云影像选项已变化，请刷新后重新选择。', status=409))
    return _private(response)


@login_required
@require_POST
def revoke(request, patient_id, share_id):
    revoke_share(patient_id, request.user, share_id)
    return redirect("patients_family:shares", patient_id=patient_id)


@require_GET
def open_link(request):
    return _private(render(request, "patients/share_landing.html"))


@require_POST
@sensitive_post_parameters("token")
@sensitive_variables()
def exchange(request):
    if not request.user.is_authenticated:
        return _private(JsonResponse({"error": "请先登录后查看分享。", "login_url": reverse("accounts:login") + "?" + urlencode({"next": reverse("shared:open")})}, status=401))
    try:
        grant = exchange_share_token(request.POST.get("token"), request.user, request.session.session_key)
    except ShareUnavailable:
        return _private(JsonResponse({"error": str(ShareUnavailable())}, status=410))
    except PermissionDenied:
        return _private(JsonResponse({"error": "登录状态已变化，请重新登录。"}, status=401))
    return _private(JsonResponse({"share_id": str(grant.share_id), "redirect": reverse("shared:detail", args=[grant.share_id])}))


def _shared_view(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(reverse("accounts:login") + "?" + urlencode({"next": reverse("shared:open")}))
        try:
            return view(request, *args, **kwargs)
        except ShareUnavailable:
            return _private(render(request, "patients/share_unavailable.html", status=410))
        except PermissionDenied:
            raise Http404("分享不可用。") from None
    return wrapped


def _access(request, share_id, document_id=None, *, sources=False, download=False):
    access = authorize_share(share_id, request.user, request.session.session_key,
                             document_id=document_id, sources=sources, download=download)
    request.share_access = access
    return access


@_shared_view
@require_GET
def detail(request, share_id):
    access = _access(request, share_id)
    from apps.glucose.output import shared_rows
    from apps.lesions.output_presentation import card_sections as lesion_sections
    response = _private(render(request, "patients/shared_detail.html", {
        "share": access.share, "snapshot": access.share.snapshot, "glucose_rows": shared_rows(access.share.snapshot),
        'lesion_sections': lesion_sections(access.share.snapshot),
    }))
    _access(request, share_id)
    return response


@_shared_view
@require_GET
def status(request, share_id):
    _access(request, share_id)
    return _private(JsonResponse({"available": True}))


def _document(request, share_id, document_id):
    access = _access(request, share_id, document_id, sources=True)
    return access, get_object_or_404(Document, pk=document_id, patient_id=access.share.patient_id, deleted_at__isnull=True)


@_shared_view
@require_GET
def document(request, share_id, document_id):
    access, selected = _document(request, share_id, document_id)
    response = _private(render(request, "patients/shared_document.html", {
        "share": access.share, "document": selected, "page_numbers": range(1, selected.page_count + 1),
    }))
    _access(request, share_id, document_id, sources=True)
    return response


@_shared_view
@require_GET
def page_image(request, share_id, document_id, page_number):
    _, selected = _document(request, share_id, document_id)
    if not 1 <= page_number <= selected.page_count:
        raise Http404
    try:
        with closing(get_object_store().open_private(selected.original_object_key)) as source:
            payload = render_page(source, selected.content_type, page_number, thumbnail=request.GET.get("thumbnail") == "1")
    except (UploadDomainError, ImproperlyConfigured, OSError, PreviewUnavailable):
        return _private(HttpResponse("来源暂时无法打开，请稍后重试。", status=503))
    _access(request, share_id, document_id, sources=True)
    return _private(HttpResponse(payload, content_type="image/png"))


@_shared_view
@require_GET
def thumbnail_sheet(request, share_id, document_id):
    _, selected = _document(request, share_id, document_id)
    try:
        with closing(get_object_store().open_private(selected.original_object_key)) as source:
            payload = render_thumbnail_sheet(source, selected.content_type, selected.page_count)
    except (UploadDomainError, ImproperlyConfigured, OSError, PreviewUnavailable):
        return _private(HttpResponse("缩略图暂时无法打开。", status=503))
    _access(request, share_id, document_id, sources=True)
    return _private(HttpResponse(payload, content_type="image/png"))


@_shared_view
@require_GET
def original(request, share_id, document_id):
    access, selected = _document(request, share_id, document_id)
    if not access.share.allow_original_download:
        return _private(HttpResponse("此分享没有开放原件下载。", status=403))
    try:
        source = get_object_store().open_private(selected.original_object_key)
    except (UploadDomainError, ImproperlyConfigured, OSError):
        return _private(HttpResponse("原件暂时无法打开。", status=503))
    try:
        _access(request, share_id, document_id, sources=True, download=True)
        def check():
            _access(request, share_id, document_id, sources=True, download=True)
        extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/heic": "heic"}[selected.content_type]
        response = guarded_file_response(FileResponse(GuardedStream(source, check), as_attachment=True,
                                                      filename="original." + extension, content_type=selected.content_type))
    except Exception:
        source.close()
        raise
    _private(response)
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response
