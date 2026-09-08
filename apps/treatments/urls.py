from django.urls import path
from . import views

app_name = "treatments"
urlpatterns = [
    path("", views.index, name="index"),
    path("events/new/", views.event_new, name="event_new"),
    path("events/<uuid:event_id>/", views.event_detail, name="event"),
    path("regimens/new/", views.regimen_new, name="regimen_new"),
    path("regimens/<uuid:regimen_id>/", views.regimen_detail, name="regimen"),
    path("cycles/new/", views.cycle_new, name="cycle_new"),
    path("cycles/merge/", views.merge, name="merge"),
    path("cycles/<uuid:cycle_id>/", views.cycle_detail, name="cycle"),
    path("cycles/<uuid:cycle_id>/split/", views.split, name="split"),
    path("cycles/<uuid:cycle_id>/records/", views.assign, name="assign"),
]
