from django.urls import path

from . import clinical_views, laterality_views, views

app_name = "facts"
urlpatterns = [
    path("", views.fact_index, name="index"),
    path("documents/<uuid:document_id>/", views.document_facts, name="document"),
    path("documents/<uuid:document_id>/reports/", clinical_views.document_reports, name="reports"),
    path("reports/<uuid:report_id>/", clinical_views.report_detail, name="report"),
    path('scope-operations/<uuid:scope_operation_id>/', laterality_views.scope_operation, name='scope_operation'),
    path('<uuid:fact_id>/scope/', laterality_views.scope_change, name='scope_change'),
    path("<uuid:fact_id>/", views.fact_detail, name="detail"),
]
