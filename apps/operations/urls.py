from django.urls import path

from . import views


app_name = "operations"

urlpatterns = [
    path("health/live/", views.live, name="live"),
    path("health/ready/", views.ready, name="ready"),
    path("internal/metrics/", views.metrics, name="metrics"),
    path("internal/alerts/", views.alerts, name="alerts"),
]
