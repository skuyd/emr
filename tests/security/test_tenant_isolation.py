import pytest
from django.http import Http404, HttpResponse
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection

from apps.core.decorators import patient_required
from apps.core.tenant import get_patient_object_or_404, get_request_patient
from apps.patients.models import Patient
from apps.patients.services import create_patient_space


CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}
EVIDENCE = {"ip": "127.0.0.1", "user_agent": "tenant-test"}


def patient_for(account, name="患者"):
    return create_patient_space(account, name, CONFIRMATIONS, EVIDENCE)


@pytest.mark.django_db
def test_account_cannot_resolve_another_accounts_patient_and_missing_is_identical_404(django_user_model):
    account_a = django_user_model.objects.create(phone_hash="a" * 64, phone_encrypted="ciphertext")
    account_b = django_user_model.objects.create(phone_hash="b" * 64, phone_encrypted="ciphertext")
    patient_b = patient_for(account_b)
    request = RequestFactory().get("/")
    request.user = account_a

    with pytest.raises(Http404) as cross_account:
        get_patient_object_or_404(Patient.objects.all(), request, patient_b.pk)
    with pytest.raises(Http404) as missing:
        get_patient_object_or_404(Patient.objects.all(), request, "00000000-0000-0000-0000-000000000000")
    with pytest.raises(Http404) as malformed:
        get_patient_object_or_404(Patient.objects.all(), request, "not-a-uuid")

    assert str(cross_account.value) == str(missing.value)
    assert str(missing.value) == str(malformed.value)


@pytest.mark.django_db
def test_current_patient_and_uuid_resolution_scope_account_id_in_sql(django_user_model):
    account = django_user_model.objects.create(phone_hash="c" * 64, phone_encrypted="ciphertext")
    patient = patient_for(account)
    request = RequestFactory().get("/")
    request.user = account

    with CaptureQueriesContext(connection) as captured:
        assert get_request_patient(request).pk == patient.pk
        assert get_patient_object_or_404(Patient.objects.all(), request, patient.pk).pk == patient.pk

    sql = "\n".join(query["sql"].lower() for query in captured.captured_queries)
    assert "account_id" in sql
    assert "id" in sql


@pytest.mark.django_db
def test_current_patient_resolver_does_not_trust_cached_reverse_relation(django_user_model):
    account_a = django_user_model.objects.create(phone_hash="d" * 64, phone_encrypted="ciphertext")
    account_b = django_user_model.objects.create(phone_hash="e" * 64, phone_encrypted="ciphertext")
    patient_a = patient_for(account_a, "甲")
    patient_b = patient_for(account_b, "乙")
    account_a._state.fields_cache["patient"] = patient_b
    request = RequestFactory().get("/")
    request.user = account_a

    assert get_request_patient(request).pk == patient_a.pk


@pytest.mark.django_db
def test_resolvers_reject_unauthenticated_and_inactive_users(django_user_model):
    from django.contrib.auth.models import AnonymousUser

    account = django_user_model.objects.create(phone_hash="f" * 64, phone_encrypted="ciphertext", is_active=False)
    for user in (AnonymousUser(), account):
        request = RequestFactory().get("/")
        request.user = user
        with pytest.raises(Http404):
            get_request_patient(request)


@pytest.mark.django_db
def test_patient_required_decorator_attaches_query_resolved_patient_and_preserves_login_redirect(django_user_model):
    account = django_user_model.objects.create(phone_hash="g" * 64, phone_encrypted="ciphertext")
    patient = patient_for(account)

    @patient_required
    def protected(request):
        return HttpResponse(str(request.patient.pk))

    anonymous_request = RequestFactory().get("/records/")
    from django.contrib.auth.models import AnonymousUser

    anonymous_request.user = AnonymousUser()
    assert protected(anonymous_request).status_code == 302

    request = RequestFactory().get("/records/")
    request.user = account
    response = protected(request)
    assert response.status_code == 200
    assert response.content.decode() == str(patient.pk)


@pytest.mark.django_db
def test_patient_required_returns_policy_503_before_attaching_patient(django_user_model, settings):
    account = django_user_model.objects.create(phone_hash="h" * 64, phone_encrypted="ciphertext")
    patient_for(account)
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["privacy"]["digest"] = "a" * 64

    @patient_required
    def protected(request):
        return HttpResponse("unexpected")

    request = RequestFactory().get("/records/")
    request.user = account
    with override_settings(CONSENT_POLICIES=policies):
        response = protected(request)
    assert response.status_code == 503
    assert not hasattr(request, "patient")
