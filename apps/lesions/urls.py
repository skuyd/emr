from django.urls import path

from . import views


app_name = "lesions"
urlpatterns = [
    path("", views.index, name="index"),
    path("propose/", views.generate, name="generate"),
    path("maximum/", views.maximum, name="maximum"),
    path("match/", views.match, name="match"),
    path("reports/<uuid:report_id>/<str:entity_key>/", views.observation, name="observation"),
    path("proposals/<uuid:lesion_proposal_id>/", views.proposal, name="proposal"),
    path("operations/<uuid:lesion_operation_id>/", views.operation, name="operation"),
    path("operations/<uuid:lesion_operation_id>/undo/", views.undo, name="undo"),
    path("<uuid:lesion_id>/", views.detail, name="detail"),
    path("<uuid:lesion_id>/rename/", views.rename, name="rename"),
    path("<uuid:lesion_id>/observations/", views.manage, name="manage"),
]
