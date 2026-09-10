from django.urls import path

from . import share_views as views
from apps.cloud_imaging import shared_views as cloud_views

app_name = "shared"
urlpatterns = [
    path('<uuid:share_id>/cloud-imaging/<uuid:source_id>/visit/', cloud_views.visit, name='cloud_imaging_visit'),
    path('<uuid:share_id>/cloud-imaging/<uuid:source_id>/open/', cloud_views.open, name='cloud_imaging_open'),
    path("open/", views.open_link, name="open"),
    path("exchange/", views.exchange, name="exchange"),
    path("<uuid:share_id>/", views.detail, name="detail"),
    path("<uuid:share_id>/status/", views.status, name="status"),
    path("<uuid:share_id>/documents/<uuid:document_id>/", views.document, name="document"),
    path("<uuid:share_id>/documents/<uuid:document_id>/pages/<int:page_number>/image/", views.page_image, name="page_image"),
    path("<uuid:share_id>/documents/<uuid:document_id>/thumbnails/", views.thumbnail_sheet, name="thumbnails"),
    path("<uuid:share_id>/documents/<uuid:document_id>/original/", views.original, name="original"),
]
