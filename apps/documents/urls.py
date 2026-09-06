from django.urls import path

from . import views
from .views.recycle_bin import recycle_bin, document_restore, document_permanent_delete


app_name = "documents"

urlpatterns = [
    path("recycle-bin/", recycle_bin, name="recycle_bin"),
    path("recycle-bin/<uuid:document_id>/restore/", document_restore, name="document_restore"),
    path("recycle-bin/<uuid:document_id>/delete/", document_permanent_delete, name="document_permanent_delete"),
    path("uploads/new/", views.upload_page, name="upload"),
    path("trends/", views.trend_index, name="trend_index"),
    path("trends/<str:standard_code>/", views.indicator_trend, name="indicator_trend"),
    path("records/", views.record_list, name="records"),
    path("records/<uuid:document_id>/", views.document_summary, name="document_summary"),
    path("records/<uuid:document_id>/feedback/", views.document_feedback, name="document_feedback"),
    path("records/<uuid:document_id>/reprocess/", views.document_reprocess, name="document_reprocess"),
    path("records/<uuid:document_id>/delete/", views.document_delete, name="document_delete"),
    path("records/<uuid:document_id>/viewer/", views.document_viewer, name="document_viewer"),
    path(
        "records/<uuid:document_id>/pages/<int:page_number>/image/",
        views.document_page_image,
        name="document_page_image",
    ),
    path(
        "records/<uuid:document_id>/thumbnails/sheet/",
        views.document_thumbnail_sheet,
        name="document_thumbnail_sheet",
    ),
    path("records/<uuid:document_id>/original/", views.document_original, name="document_original"),
    path("api/upload-batches/", views.create_batch, name="create_batch"),
    path(
        "api/upload-batches/<uuid:batch_id>/items/<uuid:item_id>/content/",
        views.upload_item_content,
        name="upload_item_content",
    ),
    path(
        "api/upload-batches/<uuid:batch_id>/items/<uuid:item_id>/remove/",
        views.remove_upload_item,
        name="remove_upload_item",
    ),
    path("api/upload-batches/<uuid:batch_id>/status/", views.batch_status, name="batch_status"),
]
