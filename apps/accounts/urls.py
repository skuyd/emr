from django.urls import path
from . import views

urlpatterns = [path("", views.login_page), path("request-code/", views.request_code), path("verify/", views.verify_code), path("logout/", views.logout_view)]
