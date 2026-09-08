from django.contrib import admin
from django.urls import include, path

from apps.accounts import views as account_views
from apps.core import views as core_views
from apps.patients import views as patient_views

urlpatterns = [
    path("self-records/", include("apps.self_records.urls")),
    path("glucose/", include("apps.glucose.urls")),
    path("shared/", include("apps.patients.share_urls")),
    path("family/invitation/", include("apps.patients.invitation_urls")),
    path("patients/", include("apps.patients.family_urls")),
    path("visit/", include("apps.exports.urls")),
    path("facts/", include("apps.facts.urls")),
    path("treatments/", include("apps.treatments.urls")),
    path("labs/", include("apps.labs.urls")),
    path("", include("apps.operations.urls")),
    path("", include("apps.notifications.urls")),
    path("", include("apps.documents.urls")),
    path("", patient_views.home, name="home"),
    path("me/", include("apps.patients.profile_urls")),
    path("tasks/", patient_views.tasks_placeholder, name="tasks"),
    path("onboarding/", include("apps.patients.urls")),
    path("login/", include("apps.accounts.urls")),
    path("logout/", account_views.logout_view),
    path("privacy/", account_views.privacy_page, name="privacy"),
    path("account-deleted/", patient_views.account_deleted, name="account_deleted"),
    path("favicon.ico", core_views.favicon, name="favicon"),
    path("admin/", admin.site.urls),
]
