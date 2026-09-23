from datetime import date

import pytest
from freezegun import freeze_time

from apps.patients.forms import OnboardingForm
from apps.patients.models import PatientMembership
from apps.operations.models import AuditEvent
from apps.operations.audit import _hash
from tests.documents.test_detail_viewer import _patient


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('field,value', [
    ('birth_date', '2026-02-30'), ('birth_date', '2001-09'),
    ('birth_date', '2026-09-23'), ('sex', 'invalid'),
    ('birth_date', ''), ('sex', ''),
])
@freeze_time('2026-09-22')
def test_onboarding_rejects_invalid_demographics(field, value):
    data = dict(display_name='患者', sex='F', birth_date='2001-09-22',
                privacy=True, sensitive_data=True, upload_authority=True)
    data[field] = value
    form = OnboardingForm(data)
    assert not form.is_valid()
    assert field in form.errors


def test_existing_patient_information_is_unknown(django_user_model):
    _, patient = _patient(django_user_model, 'demographics-unknown')
    assert patient.sex == ''
    assert patient.birth_date is None


def test_demographics_can_be_supplemented_and_corrected(django_user_model):
    client, patient = _patient(django_user_model, 'demographics-edit')
    response = client.post('/me/demographics/', {'sex': 'F', 'birth_date': '2001-09-22'})
    assert response.status_code == 302
    patient.refresh_from_db()
    assert (patient.sex, patient.birth_date) == ('F', date(2001, 9, 22))
    event = AuditEvent.objects.get(action='patient_demographics_changed')
    assert event.patient_hash == _hash('patient', patient.pk)
    assert event.resource_type == 'patient'
    assert 'name="birth_date"' in client.get('/me/').content.decode()
    assert client.post('/me/demographics/', {'sex': 'M', 'birth_date': '2000-02-29'}).status_code == 302
    patient.refresh_from_db()
    assert (patient.sex, patient.birth_date) == ('M', date(2000, 2, 29))


def test_invalid_update_preserves_saved_demographics(django_user_model):
    client, patient = _patient(django_user_model, 'demographics-invalid')
    assert client.post('/me/demographics/', {'sex': 'F', 'birth_date': '2001-09-22'}).status_code == 302
    response = client.post('/me/demographics/', {'sex': 'other', 'birth_date': '2001-09'})
    assert response.status_code == 400
    patient.refresh_from_db()
    assert (patient.sex, patient.birth_date) == ('F', date(2001, 9, 22))


def test_readonly_member_cannot_change_demographics(django_user_model):
    owner, patient = _patient(django_user_model, 'demographics-owner')
    viewer, other = _patient(django_user_model, 'demographics-viewer')
    PatientMembership.objects.create(patient=patient, account=other.account, role='VIEWER')
    session = viewer.session
    session['active_patient_id'] = str(patient.pk)
    session.save()
    response = viewer.post('/me/demographics/', {'sex': 'M', 'birth_date': '2001-09-22'})
    assert response.status_code == 403
    patient.refresh_from_db()
    assert patient.sex == '' and patient.birth_date is None


@pytest.mark.parametrize('path', ['/onboarding/', '/patients/new/'])
@pytest.mark.parametrize('missing', ['sex', 'birth_date'])
def test_new_patient_requires_demographics_without_creating_partial_record(client, django_user_model, path, missing):
    from apps.accounts.models import ConsentRecord
    from apps.patients.models import Patient
    if path == '/patients/new/':
        client, old_patient = _patient(django_user_model, 'new-required-' + missing)
    else:
        account = django_user_model.objects.create(phone_hash='e' * 64, phone_encrypted='ciphertext')
        client.force_login(account)
    data = dict(display_name='新患者', sex='F', birth_date='2000-02-29',
                privacy='on', sensitive_data='on', upload_authority='on')
    data.pop(missing)
    before = (Patient.objects.count(), ConsentRecord.objects.count())
    response = client.post(path, data)
    assert response.status_code in (200, 400)
    assert missing in response.context['form'].errors
    assert (Patient.objects.count(), ConsentRecord.objects.count()) == before


@pytest.mark.parametrize('path', ['/onboarding/', '/patients/new/'])
def test_new_patient_saves_required_demographics(client, django_user_model, path):
    from apps.patients.models import Patient
    if path == '/patients/new/':
        client, old_patient = _patient(django_user_model, 'new-saves-demographics')
    else:
        account = django_user_model.objects.create(phone_hash='f' * 64, phone_encrypted='ciphertext')
        client.force_login(account)
    page = client.get(path).content.decode()
    assert 'name="sex"' in page and 'name="birth_date"' in page
    response = client.post(path, dict(display_name='新患者', sex='F', birth_date='2000-02-29',
                                     privacy='on', sensitive_data='on', upload_authority='on'))
    assert response.status_code == 302
    patient = Patient.objects.get(display_name='新患者')
    assert (patient.sex, patient.birth_date) == ('F', date(2000, 2, 29))


def test_legacy_patient_can_keep_demographics_blank(django_user_model):
    client, patient = _patient(django_user_model, 'legacy-blank')
    assert client.post('/me/demographics/', {'sex': '', 'birth_date': ''}).status_code == 302
    patient.refresh_from_db()
    assert patient.sex == '' and patient.birth_date is None
