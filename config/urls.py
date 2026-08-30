from django.contrib import admin
from django.urls import include, path

from apps.accounts import views as account_views
from apps.patients import views as patient_views

urlpatterns = [
    path("", patient_views.home, name="home"),
    path("records/", patient_views.records_placeholder, name="records"),
    path("me/", patient_views.profile_placeholder, name="profile"),
    path("tasks/", patient_views.tasks_placeholder, name="tasks"),
    path("onboarding/", include("apps.patients.urls")),
    path("login/", include("apps.accounts.urls")),
    path("logout/", account_views.logout_view),
    path("privacy/", account_views.privacy_page, name="privacy"),
    path("admin/", admin.site.urls),
]
