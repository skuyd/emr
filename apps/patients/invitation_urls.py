from django.urls import path

from . import invitation_views as views

app_name = "family_invitation"
urlpatterns = [
    path("", views.landing, name="landing"),
    path("inspect/", views.inspect, name="inspect"),
    path("accept/", views.accept, name="accept"),
]
