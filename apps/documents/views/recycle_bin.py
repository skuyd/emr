from django.db.models import Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.core.decorators import patient_required
from apps.core.responses import protect_sensitive_html

from ..lifecycle import LifecycleUnavailable, permanently_delete_from_trash, restore_document
from ..models import Document
from ..tasks import safe_enqueue_document_deletion


def _trash(patient):
    return Document.objects.filter(patient=patient, trashed_at__isnull=False).order_by("-trashed_at", "pk")


def _render_bin(request, *, error="", status=200):
    documents = list(_trash(request.patient))
    now = timezone.now()
    for document in documents:
        document.can_restore = document.trash_expires_at > now
    retained = Document.objects.filter(patient=request.patient, deleted_at__isnull=False)
    return protect_sensitive_html(render(request, "documents/recycle_bin.html", {
        "documents": documents, "retained_bytes": retained.aggregate(total=Sum("byte_size"))["total"] or 0,
        "pending_deletion_count": retained.filter(trashed_at__isnull=True).count(),
        "error": error, "restored": request.GET.get("restored") == "1",
        "deletion_requested": request.GET.get("permanent") == "1", "current_section": "records",
    }, status=status))


@patient_required
@require_GET
def recycle_bin(request):
    return _render_bin(request)


@patient_required
@require_POST
def document_restore(request, document_id):
    get_object_or_404(_trash(request.patient), pk=document_id)
    try:
        restore_document(request.patient, document_id)
    except LifecycleUnavailable as error:
        return _render_bin(request, error=str(error), status=409)
    return redirect("/recycle-bin/?restored=1")


@patient_required
@require_http_methods(["GET", "POST"])
def document_permanent_delete(request, document_id):
    document = get_object_or_404(_trash(request.patient), pk=document_id)
    if request.method == "POST" and request.POST.get("confirmation") == "permanent":
        try:
            permanently_delete_from_trash(request.patient, document_id, dispatch=safe_enqueue_document_deletion)
        except LifecycleUnavailable:
            raise Http404("Document not found") from None
        return redirect("/recycle-bin/?permanent=1")
    return protect_sensitive_html(render(request, "documents/permanent_delete_confirm.html", {
        "document": document, "current_section": "records",
        "confirmation_error": request.method == "POST",
    }, status=400 if request.method == "POST" else 200))
