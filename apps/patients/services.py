import hashlib
import hmac
import ipaddress
import unicodedata

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.accounts.models import ConsentRecord

from .models import Patient


REQUIRED_CONSENT_TYPES = ("privacy", "sensitive_data", "upload_authority")


class MissingRequiredConsent(ValueError):
    pass


MissingConsent = MissingRequiredConsent


def normalize_display_name(value):
    if not isinstance(value, str):
        raise ValueError("Invalid display name")
    normalized_input = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized_input):
        raise ValueError("Invalid display name")
    normalized = normalized_input.strip()
    if not normalized:
        raise ValueError("Invalid display name")
    visible_count = sum(not unicodedata.category(character).startswith("M") for character in normalized)
    if not 1 <= visible_count <= 20:
        raise ValueError("Invalid display name")
    return normalized


def _request_evidence_hash(domain, value):
    secret = settings.ACCOUNTS_CRYPTO_SECRET.encode("utf-8")
    message = f"family-phr/consent-evidence/{domain}/v1:{value}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _hash_request_evidence(request_evidence):
    try:
        canonical_ip = ipaddress.ip_address(request_evidence.get("ip", "")).compressed
    except ValueError as error:
        raise ValueError("Invalid request evidence") from error
    user_agent = request_evidence.get("user_agent", "")
    if not isinstance(user_agent, str):
        raise ValueError("Invalid request evidence")
    return _request_evidence_hash("ip", canonical_ip), _request_evidence_hash("user-agent", user_agent)


def _policy_definitions():
    policies = settings.CONSENT_POLICIES
    return {consent_type: policies[consent_type] for consent_type in REQUIRED_CONSENT_TYPES}


def missing_current_consents(account):
    active_records = ConsentRecord.objects.filter(account=account, withdrawn_at__isnull=True)
    existing = {(record.consent_type, record.policy_version, record.policy_digest) for record in active_records}
    return [
        consent_type
        for consent_type, policy in _policy_definitions().items()
        if (consent_type, policy["version"], policy["digest"]) not in existing
    ]


def account_needs_onboarding(account):
    try:
        account.patient
    except Patient.DoesNotExist:
        return True
    return bool(missing_current_consents(account))


def create_patient_space(account, display_name, confirmations, request_evidence):
    if any(confirmations.get(consent_type) is not True for consent_type in REQUIRED_CONSENT_TYPES):
        raise MissingRequiredConsent("Missing required consent")
    request_ip_hash, user_agent_hash = _hash_request_evidence(request_evidence)
    normalized_name = normalize_display_name(display_name)
    policies = _policy_definitions()

    with transaction.atomic():
        locked_account = get_user_model().objects.select_for_update().get(pk=account.pk)
        patient, _ = Patient.objects.get_or_create(
            account=locked_account,
            defaults={"display_name": normalized_name},
        )
        active_records = ConsentRecord.objects.filter(account=locked_account, withdrawn_at__isnull=True)
        existing = {(record.consent_type, record.policy_version, record.policy_digest) for record in active_records}
        for consent_type, policy in policies.items():
            identity = (consent_type, policy["version"], policy["digest"])
            if identity not in existing:
                ConsentRecord.objects.create(
                    account=locked_account,
                    consent_type=consent_type,
                    policy_version=policy["version"],
                    policy_digest=policy["digest"],
                    request_ip_hash=request_ip_hash,
                    user_agent_hash=user_agent_hash,
                )
    return patient
