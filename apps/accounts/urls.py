from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("", views.login_page, name="login"),
    path("forgot-password/", views.forgot_password, name="forgot_password"),
    path("forgot-password/verify/", views.verify_password_reset, name="verify_password_reset"),
    path("forgot-password/new-password/", views.new_password, name="new_password"),
    path("first-use/", views.first_use_phone, name="first_use_phone"),
    path("first-use/verify/", views.verify_first_use, name="verify_first_use"),
    path("first-use/password/", views.first_use_password, name="first_use_password"),
    path("password/", views.password_login, name="password_login"),
    path("verify/", views.verify_login, name="verify_login"),
    path("logout/", views.logout_view, name="logout"),
]
