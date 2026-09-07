from django.urls import path
from . import family_views as views
from . import invitation_views
from . import share_views
from .audit_views import audit_history

app_name = "patients_family"
urlpatterns = [
    path("", views.patient_list, name="list"),
    path("new/", views.create_patient, name="create"),
    path("<uuid:patient_id>/select/", views.select_patient, name="select"),
    path("<uuid:patient_id>/members/", views.members, name="members"),
    path("<uuid:patient_id>/audit/", audit_history, name="audit"),
    path("<uuid:patient_id>/invitations/", invitation_views.invitations, name="invitations"),
    path("<uuid:patient_id>/invitations/<uuid:invitation_id>/revoke/", invitation_views.revoke, name="revoke_invitation"),
    path("<uuid:patient_id>/shares/", share_views.shares, name="shares"),
    path("<uuid:patient_id>/shares/<uuid:share_id>/revoke/", share_views.revoke, name="revoke_share"),
    path("<uuid:patient_id>/delete/", views.delete_patient, name="delete"),
]
