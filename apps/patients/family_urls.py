from django.urls import path
from . import family_views as views

app_name = "patients_family"
urlpatterns = [
    path("", views.patient_list, name="list"),
    path("new/", views.create_patient, name="create"),
    path("<uuid:patient_id>/select/", views.select_patient, name="select"),
    path("<uuid:patient_id>/members/", views.members, name="members"),
    path("<uuid:patient_id>/delete/", views.delete_patient, name="delete"),
]
