from django.urls import path

from . import clinical_views, views

app_name = "facts"
urlpatterns = [
    path("", views.fact_index, name="index"),
    path("documents/<uuid:document_id>/", views.document_facts, name="document"),
    path("documents/<uuid:document_id>/reports/", clinical_views.document_reports, name="reports"),
    path("reports/<uuid:report_id>/", clinical_views.report_detail, name="report"),
    path("<uuid:fact_id>/", views.fact_detail, name="detail"),
]
