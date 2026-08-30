from django.urls import path

from . import views


app_name = "documents"

urlpatterns = [
    path("uploads/new/", views.upload_page, name="upload"),
    path("records/<uuid:document_id>/", views.document_summary, name="document_summary"),
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
