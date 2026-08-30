from django.urls import path

from . import views


app_name = "notifications"

urlpatterns = [
    path("api/notifications/", views.notification_list, name="list"),
    path(
        "api/notifications/<uuid:notification_id>/read/",
        views.mark_notification_read,
        name="mark_read",
    ),
    path(
        "notifications/<uuid:notification_id>/open/",
        views.open_notification,
        name="open",
    ),
    path("api/push-subscriptions/", views.create_push_subscription, name="subscribe"),
    path(
        "api/push-subscriptions/revoke/",
        views.revoke_push_subscription,
        name="revoke",
    ),
    path("service-worker.js", views.service_worker, name="service_worker"),
]

