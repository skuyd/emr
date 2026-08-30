from django.urls import path

from . import views


app_name = "patient_profile"

urlpatterns = [
    path("", views.profile, name="profile"),
    path("name/", views.update_profile_name, name="update_name"),
    path("feedback/", views.submit_product_feedback, name="feedback"),
    path("notifications/", views.update_notification_preference, name="notifications"),
    path("delete-account/", views.delete_account, name="delete_account"),
]
