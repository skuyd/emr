from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.login_page, name="login"),
    path("password/", views.password_login, name="password_login"),
    path("verify/", views.verify_login, name="verify_login"),
    path("logout/", views.logout_view, name="logout"),
]
