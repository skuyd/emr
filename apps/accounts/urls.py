from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.login_page, name="login"),
    path("request-code/", views.request_code, name="request_code"),
    path("verify/", views.verify_code, name="verify"),
    path("logout/", views.logout_view, name="logout"),
]
