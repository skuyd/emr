import pytest

from apps.patients.forms import DisplayNameForm, OnboardingForm
from tests.documents.test_detail_viewer import _patient


@pytest.mark.parametrize("form_class", [DisplayNameForm, OnboardingForm])
@pytest.mark.parametrize("name", ["Synthetic second patient", "\u200b", "A\x00B"])
def test_invalid_patient_name_is_a_field_error(form_class, name):
    form = form_class({"display_name": name, "privacy": "on", "sensitive_data": "on", "upload_authority": "on"})
    assert not form.is_valid()
    assert "display_name" in form.errors


@pytest.mark.django_db
def test_long_name_create_and_rename_return_form_errors_without_mutation(django_user_model):
    from apps.patients.models import Patient

    client, patient = _patient(django_user_model, "invalid-patient-name")
    before = patient.display_name
    data = {"display_name": "Synthetic second patient", "upload_authority": "on"}
    assert client.post("/patients/new/", data).status_code == 400
    assert Patient.objects.filter(account=patient.account).count() == 1
    assert client.post("/me/name/", data).status_code == 400
    patient.refresh_from_db()
    assert patient.display_name == before
