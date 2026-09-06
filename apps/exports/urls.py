from django.urls import path

from . import views

app_name = "exports"
urlpatterns = [
    path("", views.prepare, name="prepare"),
    path("<uuid:job_id>/", views.preview, name="preview"),
    path("<uuid:job_id>/pdf/", views.preview_pdf, name="pdf"),
    path("<uuid:job_id>/download/", views.download, name="download"),
    path("<uuid:job_id>/cancel/", views.cancel, name="cancel"),
]
