from django.urls import path

from . import views

app_name = "patients"

urlpatterns = [
    path("", views.onboarding, name="onboarding"),
    path("sensitive-information/", views.sensitive_information, name="sensitive_information"),
]
