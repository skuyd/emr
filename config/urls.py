from django.contrib import admin
from django.urls import path
from django.urls import include

urlpatterns = [
    path("login/", include("apps.accounts.urls")),
    path("logout/", __import__("apps.accounts.views", fromlist=["logout_view"]).logout_view),
    path("admin/", admin.site.urls),
]
