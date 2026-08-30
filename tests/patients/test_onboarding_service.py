import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from apps.accounts.models import ConsentRecord
from apps.patients.models import Patient
from apps.patients.services import (
    ConsentPolicyConflict,
    MissingRequiredConsent,
    create_patient_space,
    normalize_display_name,
)
from apps.patients.policies import policy_items


REQUEST_EVIDENCE = {"ip": "2001:0db8:0:0:0:0:0:1", "user_agent": "test browser"}
CONFIRMATIONS = {"privacy": True, "sensitive_data": True, "upload_authority": True}


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("  \u00c9milie  ", "\u00c9milie"),
        ("e\u0301", "\u00e9"),
        ("\u59d3" * 20, "\u59d3" * 20),
    ],
)
def test_display_name_is_trimmed_and_normalized_to_nfc(submitted, expected):
    assert normalize_display_name(submitted) == expected


@pytest.mark.parametrize("submitted", ["", "   ", "\n\u59d3\u540d", "\u59d3\u200d\u540d", "\u59d3" * 21, "a" + "\u0301" * 81])
def test_display_name_rejects_blank_controls_format_characters_and_over_twenty_visible_characters(submitted):
    with pytest.raises(ValueError):
        normalize_display_name(submitted)


@pytest.mark.django_db
def test_missing_confirmation_rejects_without_creating_patient(django_user_model):
    account = django_user_model.objects.create(phone_hash="a" * 64, phone_encrypted="ciphertext")
    confirmations = {**CONFIRMATIONS, "upload_authority": False}

    with pytest.raises(MissingRequiredConsent):
        create_patient_space(account, "\u738b\u5c0f\u660e", confirmations, REQUEST_EVIDENCE)

    assert not Patient.objects.exists()
    assert not ConsentRecord.objects.exists()


@pytest.mark.django_db
def test_create_patient_space_is_idempotent_preserves_name_and_stores_only_hashed_request_evidence(django_user_model):
    account = django_user_model.objects.create(phone_hash="b" * 64, phone_encrypted="ciphertext")

    patient = create_patient_space(account, "  \u674e\u96ea  ", CONFIRMATIONS, REQUEST_EVIDENCE)
    repeated = create_patient_space(account, "\u4e0d\u5e94\u66f4\u540d", CONFIRMATIONS, REQUEST_EVIDENCE)

    assert isinstance(patient, Patient)
    assert repeated.pk == patient.pk
    assert patient.display_name == "\u674e\u96ea"
    records = list(ConsentRecord.objects.order_by("consent_type"))
    assert len(records) == 3
    assert all(record.request_ip_hash and record.user_agent_hash for record in records)
    assert all(record.request_ip_hash != record.user_agent_hash for record in records)
    assert all("2001:0db8" not in str(record.__dict__) for record in records)
    assert all("test browser" not in str(record.__dict__) for record in records)


@pytest.mark.django_db
def test_patient_one_to_one_constraint_allows_only_one_patient(django_user_model):
    account = django_user_model.objects.create(phone_hash="c" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "\u5f20\u4e09", CONFIRMATIONS, REQUEST_EVIDENCE)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Patient.objects.create(account=account, display_name="\u674e\u56db")


@pytest.mark.django_db
def test_reconsent_creates_only_new_current_policy_versions_and_preserves_history(django_user_model, settings):
    account = django_user_model.objects.create(phone_hash="d" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "\u5f20\u4e09", CONFIRMATIONS, REQUEST_EVIDENCE)
    original_count = ConsentRecord.objects.count()
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["privacy"] = {**policies["privacy"], "version": "2026-09-01"}

    with override_settings(CONSENT_POLICIES=policies):
        patient = create_patient_space(account, None, {"privacy": True}, REQUEST_EVIDENCE)
        assert patient.display_name == "\u5f20\u4e09"
        assert ConsentRecord.objects.count() == original_count + 1
        assert ConsentRecord.objects.filter(consent_type="privacy").count() == 2
        repeated = create_patient_space(account, "\u4e0d\u5e94\u66f4\u540d", CONFIRMATIONS, REQUEST_EVIDENCE)
        assert repeated.pk == patient.pk
        assert ConsentRecord.objects.count() == original_count + 1


@pytest.mark.django_db
def test_current_patient_returns_before_validating_resubmitted_name_evidence_or_confirmations(django_user_model):
    account = django_user_model.objects.create(phone_hash="g" * 64, phone_encrypted="ciphertext")
    patient = create_patient_space(account, "\u738b\u5c0f\u660e", CONFIRMATIONS, REQUEST_EVIDENCE)

    returned = create_patient_space(account, "\ninvalid", {}, {"ip": "not-an-ip", "user_agent": object()})

    assert returned.pk == patient.pk
    assert returned.display_name == "\u738b\u5c0f\u660e"
    assert ConsentRecord.objects.filter(account=account).count() == 3


@pytest.mark.django_db
def test_same_version_with_a_different_digest_raises_safe_policy_conflict(django_user_model, settings):
    account = django_user_model.objects.create(phone_hash="h" * 64, phone_encrypted="ciphertext")
    create_patient_space(account, "\u738b\u5c0f\u660e", CONFIRMATIONS, REQUEST_EVIDENCE)
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["privacy"]["digest"] = "a" * 64

    with override_settings(CONSENT_POLICIES=policies):
        with pytest.raises(ConsentPolicyConflict):
            create_patient_space(account, None, {"privacy": True}, REQUEST_EVIDENCE)


def test_malformed_canonical_policy_settings_raise_typed_conflict_before_policy_items(settings):
    policies = {key: value.copy() for key, value in settings.CONSENT_POLICIES.items()}
    policies["privacy"]["digest"] = "not-a-digest"

    with override_settings(CONSENT_POLICIES=policies):
        with pytest.raises(ConsentPolicyConflict):
            policy_items()
