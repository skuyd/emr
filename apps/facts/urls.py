from django.urls import path

from . import views

app_name = "facts"
urlpatterns = [
    path("", views.fact_index, name="index"),
    path("documents/<uuid:document_id>/", views.document_facts, name="document"),
    path("<uuid:fact_id>/", views.fact_detail, name="detail"),
]
