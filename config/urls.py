from django.contrib import admin
from django.urls import include, path

from apps.accounts import views as account_views

urlpatterns = [
    path("login/", include("apps.accounts.urls")),
    path("logout/", account_views.logout_view),
    path("privacy/", account_views.privacy_page, name="privacy"),
    path("admin/", admin.site.urls),
]
