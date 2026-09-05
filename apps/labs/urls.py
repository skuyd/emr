from django.urls import path

from . import views

app_name = "labs"
urlpatterns = [
    path("compare/", views.comparison, name="comparison"),
    path("observations/<uuid:observation_id>/", views.observation, name="observation"),
    path("observations/<uuid:observation_id>/review/", views.create_task, name="create_task"),
    path("observations/<uuid:observation_id>/source/<str:field>/", views.observation_source, name="observation_source"),
    path("observations/<uuid:observation_id>/source/<str:field>/image/", views.observation_source, {"image": True}, name="observation_source_image"),
    path("reviews/", views.review_queue, name="reviews"),
    path("reviews/<uuid:task_id>/", views.review_task, name="review_task"),
    path("reviews/<uuid:task_id>/source/<str:field>/", views.review_source, name="review_source"),
    path("reviews/<uuid:task_id>/source/<str:field>/image/", views.review_source, {"image": True}, name="review_source_image"),
    path("versions/<uuid:version_id>/activate/", views.activate_version, name="activate_version"),
    path("dictionary/", views.dictionary, name="dictionary"),
    path("dictionary/verify/", views.second_factor, name="second_factor"),
    path("dictionary/candidates/<uuid:candidate_id>/", views.dictionary_candidate, name="dictionary_candidate"),
    path("dictionary/preview/", views.dictionary_preview, name="dictionary_preview"),
    path("dictionary/publish/", views.dictionary_publish, name="dictionary_publish"),
    path("dictionary/releases/<uuid:release_id>/rollback/", views.dictionary_rollback, name="dictionary_rollback"),
]
